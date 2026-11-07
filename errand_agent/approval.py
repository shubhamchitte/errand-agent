"""Approval gate, enforced in code.

The model can only *propose*; code builds a canonical PendingAction card and its hash.
Execution requires (a) an affirmative USER reply, (b) addressed to the latest pending
action that was actually shown to the user, (c) not expired, (d) whose stored card still matches its hash
(tamper check only - real-world freshness is re-checked by the Executor). Nothing the model says counts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass

from . import db
from .clock import Clock
from .text import inr, when

PENDING, EXECUTING, DONE, FAILED = "PENDING", "EXECUTING", "DONE", "FAILED"
INVALIDATED, SUPERSEDED, DECLINED, EXPIRED = "INVALIDATED", "SUPERSEDED", "DECLINED", "EXPIRED"
APPROVAL_TTL_MIN = 30  # a PENDING card older than this (e.g. from an old session) can no longer be approved
ACTION_TYPES = ("book", "cancel", "reschedule")


@dataclass(frozen=True)
class ActionCard:
    type: str
    provider_id: str
    provider_name: str
    slot: str
    price_inr: int | None
    booking_id: str | None = None
    service: str | None = None

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def hash(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    def fingerprint(self) -> str:
        """Short visible ID of the exact card the user approves, e.g. '#a3f9'."""
        return "#" + self.hash()[:4]

    def render(self) -> str:
        price = inr(self.price_inr)
        verb = {"book": "BOOK", "cancel": "CANCEL booking", "reschedule": "RESCHEDULE booking"}[self.type]
        bk = f" {self.booking_id}" if self.booking_id else ""
        what = f"to {self.slot}" if self.type == "reschedule" else f"on {self.slot}"
        return f"[{verb}{bk}] {self.provider_name} ({self.provider_id}) {what} - price {price}"


    def friendly(self) -> str:
        """Short, plain-words version shown to the user (render() stays the canonical machine-readable form)."""
        bk = f" {self.booking_id}" if self.booking_id else ""
        who = self.provider_name
        if self.type == "book":
            return f"Book {who} on {when(self.slot)} for {inr(self.price_inr)}."
        if self.type == "cancel":
            return f"Cancel booking{bk} with {who} ({when(self.slot)})."
        return f"Move booking{bk} with {who} to {when(self.slot)} ({inr(self.price_inr)})."


@dataclass
class PendingAction:
    action_id: str
    type: str
    card: ActionCard
    hash: str
    status: str
    hold_id: str | None = None
    hold_expires_at: str | None = None
    result: dict | None = None

    def hold_seconds_left(self, clock: Clock) -> float | None:
        if not self.hold_expires_at:
            return None
        from datetime import datetime
        return (datetime.fromisoformat(self.hold_expires_at) - datetime.fromisoformat(clock.iso())).total_seconds()

    def hold_minutes_left(self, clock: Clock) -> int | None:
        """Whole minutes (rounded up) until the hold expires; None if there is no hold."""
        s = self.hold_seconds_left(clock)
        return None if s is None else max(0, -int(-s // 60))

    def hash_ok(self) -> bool:
        return self.card.hash() == self.hash


class GateError(Exception):
    """Raised when execution is attempted without valid approval."""


class ApprovalGate:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, user_id: str):
        self.conn, self.clock, self.user_id = conn, clock, user_id  # tables created by WorkflowStore DDL

    def _row_to(self, r: dict) -> PendingAction:
        return PendingAction(r["action_id"], r["type"], ActionCard(**json.loads(r["card"])), r["hash"], r["status"],
                             r["hold_id"], r["hold_expires_at"], json.loads(r["result"]) if r["result"] else None)

    def propose(self, card: ActionCard, hold_id: str | None = None, hold_expires_at: str | None = None) -> PendingAction:
        """Create a new pending action; any older PENDING one is superseded (only the latest can be approved)."""
        assert card.type in ACTION_TYPES
        now = self.clock.iso()
        self.conn.execute("UPDATE wf_pending SET status=?, updated_at=? WHERE user_id=? AND status=?",
                          (SUPERSEDED, now, self.user_id, PENDING))
        aid = "act_" + uuid.uuid4().hex[:10]
        h = card.hash()
        self.conn.execute("INSERT INTO wf_pending VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (aid, self.user_id, card.type, card.canonical(), h, PENDING, hold_id, hold_expires_at,
                           now, now, None))
        self.conn.commit()
        return self.get(aid)

    def get(self, action_id: str) -> PendingAction | None:
        r = db.one(self.conn, "SELECT * FROM wf_pending WHERE action_id=?", (action_id,))
        return self._row_to(r) if r else None

    def latest(self) -> PendingAction | None:
        r = db.one(self.conn, "SELECT * FROM wf_pending WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (self.user_id,))
        return self._row_to(r) if r else None

    def active(self) -> PendingAction | None:
        a = self.latest()
        return a if a and a.status == PENDING else None

    def set_status(self, action_id: str, status: str, result: dict | None = None) -> None:
        self.conn.execute("UPDATE wf_pending SET status=?, updated_at=?, result=COALESCE(?, result) WHERE action_id=?",
                          (status, self.clock.iso(), json.dumps(result) if result is not None else None, action_id))
        self.conn.commit()

    def update_hold(self, action_id: str, hold_id: str, expires_at: str) -> None:
        self.conn.execute("UPDATE wf_pending SET hold_id=?, hold_expires_at=? WHERE action_id=?",
                          (hold_id, expires_at, action_id))
        self.conn.commit()

    def invalidate_active(self, reason: str = "") -> PendingAction | None:
        a = self.active()
        if a:
            self.set_status(a.action_id, INVALIDATED, {"reason": reason})
        return a

    def authorize(self, user_text_is_affirmative: bool, action_id: str | None = None) -> PendingAction:
        """The only path to execution. ``user_text_is_affirmative`` must come from the user's own message."""
        if not user_text_is_affirmative:
            raise GateError("no affirmative user reply")
        a = self.latest()
        if a is None or a.status != PENDING:
            raise GateError("no pending action")
        if action_id is not None and action_id != a.action_id:
            raise GateError("approval is not for the latest pending action")
        if self._expired(a):
            self.set_status(a.action_id, EXPIRED, {"reason": "approval card too old"})
            raise GateError("this approval request has expired; please ask again")
        # NOTE: this hash check only detects tampering with the stored card between proposal and approval.
        # Freshness against the real world is checked at execution time by Executor._precheck / _prepare_hold.
        if not a.hash_ok():
            self.set_status(a.action_id, INVALIDATED, {"reason": "hash mismatch"})
            raise GateError("action card changed since it was shown")
        # atomic claim: PENDING -> EXECUTING exactly once
        cur = self.conn.execute("UPDATE wf_pending SET status=?, updated_at=? WHERE action_id=? AND status=?",
                                (EXECUTING, self.clock.iso(), a.action_id, PENDING))
        self.conn.commit()
        if cur.rowcount != 1:
            raise GateError("action already claimed")
        a.status = EXECUTING
        return a

    def _expired(self, a: PendingAction) -> bool:
        r = db.one(self.conn, "SELECT created_at FROM wf_pending WHERE action_id=?", (a.action_id,))
        if not r or not r["created_at"]:
            return False
        from datetime import datetime, timedelta
        return datetime.fromisoformat(r["created_at"]) + timedelta(minutes=APPROVAL_TTL_MIN) < \
            datetime.fromisoformat(self.clock.iso())

    def in_flight(self) -> list[PendingAction]:
        return [self._row_to(r) for r in db.rows(self.conn, "SELECT * FROM wf_pending WHERE user_id=? AND status=?",
                                                  (self.user_id, EXECUTING))]
