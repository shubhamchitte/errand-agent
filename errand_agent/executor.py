"""Executor for consequential actions (book / cancel / reschedule) + startup recovery.

Invariants:
* the idempotency key is persisted (wf_exec row, status EXECUTING) BEFORE the tool is called;
* the same approved action always uses the same key (repeat confirm / restart => no duplicate);
* ToolTimeout => outcome unknown => VERIFYING => authoritative status check; success is only
  reported once verified;
* TransientError (503) => bounded retry with the same key.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field

from . import db
from .approval import DONE as A_DONE, EXECUTING as A_EXEC, FAILED as A_FAILED, ActionCard, ApprovalGate, PendingAction
from .clock import Clock
from .mocks import ToolError, ToolTimeout, TransientError
from .trace import sanitize as _sanitize

EXECUTING, VERIFYING, DONE, FAILED, UNKNOWN = "EXECUTING", "VERIFYING", "DONE", "FAILED", "UNKNOWN"
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.25
UNVERIFIABLE = object()  # sentinel: status check failed, outcome unknown


@dataclass
class Outcome:
    status: str  # done | failed | reask | conflict | unknown
    message: str
    result: dict = field(default_factory=dict)
    new_card: ActionCard | None = None
    alternatives: list[str] = field(default_factory=list)
    hold: dict | None = None


def call_tool(env, tracer, name: str, **args):
    """Call a mock tool, tracing it. Re-raises ToolError subclasses."""
    t0 = time.monotonic()
    try:
        res = env.call(name, **args)
    except ToolError as e:
        if tracer is not None:
            tracer.tool_call(name, args, None, int((time.monotonic() - t0) * 1000), error=f"{type(e).__name__}: {e}")
        raise
    if tracer is not None:
        tracer.tool_call(name, args, _sanitize(res), int((time.monotonic() - t0) * 1000))
    return res


class Executor:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, env, gate: ApprovalGate, user_id: str,
                 tracer=None, on_user_cancel=None, timer=time.monotonic, sleep=time.sleep):
        self.conn, self.clock, self.env, self.gate, self.user_id = conn, clock, env, gate, user_id
        self.tracer = tracer
        self.on_user_cancel = on_user_cancel
        self.timer, self.sleep = timer, sleep
        self.deadline: float | None = None

    def _out_of_time(self) -> bool:
        return self.deadline is not None and self.timer() >= self.deadline

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff between retries, never sleeping past the deadline."""
        d = BACKOFF_BASE_S * 2 ** (attempt - 1)
        if self.deadline is not None:
            d = min(d, max(0.0, self.deadline - self.timer()))
        if d > 0:
            self.sleep(d)

    # ----------------------------------------------------------- helpers
    @staticmethod
    def key_for(action: PendingAction) -> str:
        return f"idem-{action.type}-{action.action_id}-{action.hash[:12]}"

    def _row(self, key: str) -> dict | None:
        return db.one(self.conn, "SELECT * FROM wf_exec WHERE idempotency_key=?", (key,))

    def _set(self, key: str, status: str, result: dict | None = None, bump: bool = False) -> None:
        self.conn.execute(
            "UPDATE wf_exec SET status=?, result=COALESCE(?, result), updated_at=?, attempts=attempts+? "
            "WHERE idempotency_key=?",
            (status, json.dumps(result) if result is not None else None, self.clock.iso(), 1 if bump else 0, key))
        self.conn.commit()

    def _call(self, name, **args):
        return call_tool(self.env, self.tracer, name, **args)

    def _event(self, type_, **data):
        if self.tracer is not None:
            self.tracer.event(type_, **data)

    # ----------------------------------------------------------- public
    def execute(self, action: PendingAction, deadline: float | None = None) -> Outcome:
        """Execute an action already authorized by ApprovalGate.authorize (status EXECUTING).
        ``deadline`` is an absolute ``timer()`` value (the agent's wall-clock budget)."""
        if action.status != A_EXEC:
            raise RuntimeError("executor called without gate authorization")
        self.deadline = deadline
        try:
            return self._execute(action)
        finally:
            self.deadline = None

    def _execute(self, action: PendingAction) -> Outcome:
        if not self._row(self.key_for(action)):
            pre = self._precheck(action)
            if pre is not None:
                return pre
        if action.type == "book":
            pre = self._prepare_hold(action)
            if pre is not None:
                return pre
            action = self.gate.get(action.action_id)
        key = self.key_for(action)
        args = self._args(action, key)
        existing = self._row(key)
        if existing and existing["status"] == DONE:
            return self._finish(action, Outcome("done", self._success_msg(action, json.loads(existing["result"])),
                                                json.loads(existing["result"])))
        if not existing:
            # persist the key BEFORE any side-effecting call
            self.conn.execute("INSERT INTO wf_exec VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (key, action.action_id, self.user_id, action.type, json.dumps(args), EXECUTING, 0, None,
                               self.clock.iso(), self.clock.iso()))
            self.conn.commit()
            self._event("idempotency_key_persisted", key=key, action_id=action.action_id)
        return self._finish(action, self._run(action, key, args))

    def _precheck(self, action: PendingAction) -> Outcome | None:
        """Before the first side-effecting call, recompute the action's facts from FRESH state:
        * book: refuse if a confirmed booking with the same provider+slot already exists (duplicate guard
          independent of action_id / idempotency key);
        * cancel/reschedule: re-read the booking and rebuild the card; if its hash differs from the approved hash
          (provider, slot, price or status changed), the approval no longer matches reality -> invalidate.
        (For book, the approved price is re-checked against the hold quote in _prepare_hold.)"""
        c = action.card
        try:
            lb = self._call("list_bookings", user_id=self.user_id)
        except ToolError:
            return None if action.type == "book" else self._fail_pre(action, "I couldn't re-check the booking "
                                                                             "before changing it")
        bks = lb.get("bookings", [])
        if action.type == "book":
            dup = next((b for b in bks if b.get("status") == "confirmed" and b["provider_id"] == c.provider_id
                        and b["slot"] == c.slot), None)
            if dup:
                self.gate.set_status(action.action_id, A_FAILED, {"reason": "duplicate", "booking_id": dup["booking_id"]})
                self._event("duplicate_refused", action_id=action.action_id, booking_id=dup["booking_id"])
                return Outcome("duplicate", f"You already have this booking: {dup['booking_id']} with {c.provider_name} "
                                            f"on {c.slot}. I did not book it again.", dup)
            return None
        b = next((x for x in bks if x["booking_id"] == c.booking_id), None)
        if b is None or b.get("status") != "confirmed":
            return self._changed(action, f"booking {c.booking_id} is no longer active")
        fresh = ActionCard(c.type, b["provider_id"], b["provider_name"],
                           b["slot"] if c.type == "cancel" else c.slot, b["price"], c.booking_id, b["service"])
        if fresh.hash() != action.hash:
            return self._changed(action, f"booking {c.booking_id} changed since you approved it "
                                         f"(now {b['provider_name']} on {b['slot']}, ₹{b['price']})")
        return None

    def _changed(self, action: PendingAction, why: str) -> Outcome:
        self.gate.set_status(action.action_id, "INVALIDATED", {"reason": why})
        self._event("approval_stale", action_id=action.action_id, reason=why)
        return Outcome("changed", f"I did not {action.type}: {why}. Nothing was changed; tell me if you still want it.")

    def _fail_pre(self, action: PendingAction, why: str) -> Outcome:
        self.gate.set_status(action.action_id, A_FAILED, {"reason": why})
        return Outcome("failed", f"{why}, so I didn't {action.type} anything. Please try again.")

    def _args(self, action: PendingAction, key: str) -> dict:
        c = action.card
        if action.type == "book":
            return {"hold_id": action.hold_id, "idempotency_key": key}
        if action.type == "cancel":
            return {"booking_id": c.booking_id, "idempotency_key": key}
        return {"booking_id": c.booking_id, "slot": c.slot, "idempotency_key": key}

    def _prepare_hold(self, action: PendingAction) -> Outcome | None:
        """If the hold expired, re-hold; any change in price/slot invalidates approval (re-ask)."""
        if action.hold_id and action.hold_expires_at and self.clock.iso() <= action.hold_expires_at:
            return None
        if self._row(self.key_for(action)):
            return None  # already attempted under this key; never swap the hold mid-flight
        c = action.card
        try:
            h = self._call("hold_slot", provider_id=c.provider_id, slot=c.slot)
        except ToolError as e:
            self.gate.set_status(action.action_id, A_FAILED, {"error": str(e)})
            return Outcome("failed", f"I couldn't re-hold {c.slot} with {c.provider_name} ({e}). Nothing was booked.")
        if h.get("status") != "held":
            self.gate.set_status(action.action_id, A_FAILED, h)
            return Outcome("failed", f"The hold expired and {c.slot} with {c.provider_name} is no longer available. "
                                     "Nothing was booked.", h)
        if h["quoted_price"] != c.price_inr or h["slot"] != c.slot:
            self.gate.set_status(action.action_id, "INVALIDATED", {"reason": "re-hold changed", "hold": h})
            new = ActionCard("book", c.provider_id, c.provider_name, h["slot"], h["quoted_price"], None, c.service)
            return Outcome("reask", f"The hold expired and the re-held price changed from ₹{c.price_inr} to "
                                    f"₹{h['quoted_price']}. I need your approval again.", h, new_card=new, hold=h)
        self.gate.update_hold(action.action_id, h["hold_id"], h["expires_at"])
        return None

    def _run(self, action: PendingAction, key: str, args: dict) -> Outcome:
        verify_needed = False
        res: dict | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._out_of_time():
                self._event("deadline_exceeded", tool=action.type, attempt=attempt)
                if verify_needed or attempt > 1:
                    return self._unknown(key, action, timed_out=True)
                self._set(key, FAILED)
                return Outcome("failed", f"I ran out of time before attempting the {action.type}. Nothing was done.")
            if attempt > 1:
                self._backoff(attempt - 1)
            self._set(key, EXECUTING, bump=True)
            try:
                res = self._call(action.type, **args)
                break
            except TransientError:
                self._event("retry", tool=action.type, attempt=attempt, reason="503")
                continue
            except ToolTimeout:
                self._set(key, VERIFYING)
                v = self.verify(action, key)
                if v is UNVERIFIABLE:
                    return self._unknown(key, action)
                if v is not None:
                    return v
                verify_needed = True  # verified NOT executed; safe to retry with the same key
                continue
        if res is None:
            self._set(key, UNKNOWN if verify_needed else FAILED)
            return Outcome("unknown" if verify_needed else "failed",
                           f"I couldn't complete the {action.type} after {MAX_ATTEMPTS} attempts "
                           "(the service kept failing). Nothing was confirmed; please try again later.")
        return self._interpret(action, key, res)

    def _interpret(self, action: PendingAction, key: str, res: dict) -> Outcome:
        st = res.get("status")
        c = action.card
        if action.type == "book":
            if st == "confirmed":
                # never claim success unverified: confirm against the authoritative status
                self._set(key, VERIFYING)
                v = self.verify(action, key)
                if v is UNVERIFIABLE:
                    return self._unknown(key, action)
                if v is not None:
                    return v
                self._set(key, FAILED, res)
                return Outcome("failed", "The booking response said confirmed but the status check disagrees; "
                                         "I am not treating it as booked.", res)
            self._set(key, FAILED, res)
            why = {"slot_taken": f"{c.slot} was just taken by someone else",
                   "hold_expired": "the hold expired before booking"}.get(st, st)
            return Outcome("failed", f"Booking failed: {why}. Nothing was booked.", res)
        if action.type == "cancel":
            if st == "cancelled":
                self._set(key, DONE, res)
                return Outcome("done", self._success_msg(action, res), res)
            self._set(key, FAILED, res)
            return Outcome("failed", f"Could not cancel {c.booking_id}: {st}.", res)
        if st == "rescheduled":
            self._set(key, DONE, res)
            return Outcome("done", self._success_msg(action, res), res)
        if st == "slot_conflict":
            self._set(key, FAILED, res)
            alts = list(res.get("alternatives") or [])
            return Outcome("conflict", f"{c.slot} is not available for {c.booking_id}."
                           + (f" Alternatives: {', '.join(alts)}." if alts else " No alternatives offered."),
                           res, alternatives=alts)
        self._set(key, FAILED, res)
        return Outcome("failed", f"Could not reschedule {c.booking_id}: {st}.", res)

    def _unknown(self, key: str, action: PendingAction, timed_out: bool = False) -> Outcome:
        self._set(key, UNKNOWN)
        if timed_out:
            return Outcome("unknown", f"I ran out of time while the {action.type} service was failing. Nothing was "
                                      "confirmed; I've flagged it for a follow-up check and will not repeat it blindly.")
        return Outcome("unknown", f"I couldn't verify whether the {action.type} went through (the status service is "
                                  "not responding). Nothing was confirmed; I will re-check automatically and will not "
                                  "repeat it blindly.")

    def verify(self, action: PendingAction, key: str):
        """Authoritative check. Returns a done Outcome if the effect happened, None if it verifiably did NOT,
        or UNVERIFIABLE if the status check itself kept failing (outcome still unknown)."""
        c = action.card
        for i in range(MAX_ATTEMPTS):
            if i:
                if self._out_of_time():
                    return UNVERIFIABLE
                self._backoff(i)
            try:
                if action.type == "book":
                    s = self._call("get_booking_status", hold_id=action.hold_id, idempotency_key=key)
                    if s.get("status") == "booked":
                        self._set(key, DONE, s)
                        return Outcome("done", self._success_msg(action, s), s)
                    return None
                lb = self._call("list_bookings", user_id=self.user_id)
                b = next((x for x in lb.get("bookings", []) if x["booking_id"] == c.booking_id), None)
                if action.type == "cancel" and b and b["status"] == "cancelled":
                    res = {"status": "cancelled", "booking_id": c.booking_id, "verified": True}
                    self._set(key, DONE, res)
                    return Outcome("done", self._success_msg(action, res), res)
                if action.type == "reschedule" and b and b["slot"] == c.slot:
                    res = {"status": "rescheduled", "booking_id": c.booking_id, "slot": c.slot, "verified": True}
                    self._set(key, DONE, res)
                    return Outcome("done", self._success_msg(action, res), res)
                return None
            except ToolError:
                continue
        return UNVERIFIABLE

    def _success_msg(self, action: PendingAction, res: dict) -> str:
        c = action.card
        if action.type == "book":
            return (f"Booked (verified): {c.provider_name} on {res.get('slot', c.slot)} for ₹{res.get('price', c.price_inr)}. "
                    f"Booking id {res.get('booking_id')}.")
        if action.type == "cancel":
            return f"Cancelled booking {c.booking_id} with {c.provider_name} (verified)."
        return f"Rescheduled booking {c.booking_id} with {c.provider_name} to {c.slot} (verified)."

    def _finish(self, action: PendingAction, out: Outcome) -> Outcome:
        if out.status == "done":
            self.gate.set_status(action.action_id, A_DONE, out.result)
            if action.type == "cancel" and self.on_user_cancel:
                self.on_user_cancel(action.card.booking_id)
        elif out.status in ("failed", "conflict"):
            self.gate.set_status(action.action_id, A_FAILED, out.result)
        elif out.status == "unknown":
            pass  # stays EXECUTING; recovery pass will resolve it
        return out

    # ----------------------------------------------------------- recovery
    def recover(self) -> list[Outcome]:
        """Startup pass: resolve every EXECUTING/VERIFYING/UNKNOWN row. Re-issuing uses the same key (idempotent)."""
        outs = []
        rows = db.rows(self.conn, "SELECT * FROM wf_exec WHERE user_id=? AND status IN (?,?,?) ORDER BY created_at",
                       (self.user_id, EXECUTING, VERIFYING, UNKNOWN))
        for r in rows:
            action = self.gate.get(r["action_id"])
            if action is None:
                continue
            self._event("recovery", key=r["idempotency_key"], status=r["status"], action_id=r["action_id"])
            v = self.verify(action, r["idempotency_key"])
            if v is UNVERIFIABLE:
                outs.append(self._unknown(r["idempotency_key"], action))
                continue
            if v is None:
                v = self._run(action, r["idempotency_key"], json.loads(r["args"]))
            if action.status != A_EXEC:
                action.status = A_EXEC
            outs.append(self._finish(action, v))
        # pending actions claimed (EXECUTING) but with no exec row: crashed before key persisted => nothing ran
        for a in self.gate.in_flight():
            if not self._row(self.key_for(a)):
                self.gate.set_status(a.action_id, A_FAILED, {"reason": "interrupted before execution"})
                outs.append(Outcome("failed", f"An approved {a.type} was interrupted before it ran; nothing was "
                                              "executed. Please confirm again if you still want it."))
        return outs
