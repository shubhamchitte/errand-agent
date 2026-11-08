"""Agent loop: router -> slots -> bounded model/tool loop -> approval gate -> executor.

The model only sees read-only tools and propose_* tools. Approval comes exclusively from the
user's own message text (``is_affirmative``) and is checked by ApprovalGate against the latest
pending action's hash. Consequential tools are called only by Executor.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import date, timedelta
from dataclasses import dataclass

from . import state as S
from .approval import DONE as A_DONE, ActionCard, ApprovalGate, DECLINED, GateError
from .context import ContextBuilder, wrap_untrusted
from .executor import Executor, call_tool
from .mocks import TOOL_SCHEMAS, ToolError, ToolTimeout, TransientError
from .mocks import data as mock_data
from .text import inr, when
from .trace import context_stat_line, estimate_cost, usd_to_inr
from .models import AGENT_TOOL_NAMES, AGENT_TOOLS, Model, ToolCall
from .ranking import rank
from .router import _area, _time, is_affirmative, is_negative, route

ACTION_INTENTS = ("new_booking", "reschedule", "cancel", "rebook_same_provider")


def parse_earliest(v: str) -> str | None:
    """'Not before 10am ...' -> '10:00'."""
    t = (v or "").lower()
    got = _time(t)
    if got:
        return got
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    return _time(f"after {m.group(0)}") if m else None


def parse_inr(v: str) -> int | None:
    """'Usually spends up to Rs 1500 ...' -> 1500 (only an amount tagged with a currency marker)."""
    m = re.search(r"(?:₹|\brs\.?|\binr)\s*(\d[\d,]*)", (v or "").lower())
    return int(m.group(1).replace(",", "")) if m else None


def parse_area(v: str) -> str | None:
    return _area((v or "").lower())


# Explicit memory-key -> (slot, parser) table. Only these exact keys seed empty slots; nothing is matched by
# substring, so e.g. a forgotten/old key such as 'old.earliest_start' can never fill a slot.
MEMORY_SLOT_MAP: dict[str, tuple[str, object]] = {
    "pref.earliest_start": ("earliest_time", parse_earliest),
    "budget.typical_visit": ("budget_inr", parse_inr),
    "area.locality": ("area", parse_area),
}


_SCHEMAS = {t["name"]: t for t in TOOL_SCHEMAS}


@dataclass
class Limits:
    max_model_turns: int = 6
    max_tool_calls: int = 10
    wall_clock_s: float = 90.0


class _NullTracer:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _compact(name: str, res: dict) -> dict:
    """Trim tool payloads before they enter the model context."""
    if name == "search_providers":
        return {"status": res.get("status"), "providers": [
            {k: p.get(k) for k in ("provider_id", "name", "area", "price_inr", "rating", "in_requested_area")}
            | {"slots": p.get("available_slots", [])[:6],
               "attrs": {k: v for k, v in (p.get("attributes") or {}).items() if k in ("verified", "licensed", "parts_included", "emergency")}}
            for p in res.get("providers", [])]}
    if name == "list_bookings":
        return {"status": res.get("status"), "bookings": res.get("bookings", [])[-10:]}
    return res


def _guess_phrase(value: str) -> str:
    """'Probably prefers late morning slots' -> 'You seem to prefer late morning slots'."""
    m = re.match(r"Probably (\w+)\s*(.*)", value)
    if not m:
        return "I'm guessing: " + value
    verb, rest = m.groups()
    if verb.endswith("s") and not verb.endswith("ss"):
        return f"You seem to {verb[:-1]} {rest}".strip()
    return f"You seem to be {verb} {rest}".strip()


class Agent:
    def __init__(self, conn, env, model: Model, clock, user_id: str = "u_demo", memory=None, tracer=None,
                 limits: Limits | None = None, timer=time.monotonic, recover: bool = True, sleep=time.sleep,
                 ask_guesses: bool = False):
        self.conn, self.env, self.model, self.clock, self.user_id = conn, env, model, clock, user_id
        self.memory = memory
        self.ask_guesses = ask_guesses
        self._t_start = time.monotonic()
        self._turn_usd = 0.0
        self.tracer = tracer if tracer is not None else _NullTracer()
        self.limits = limits or Limits()
        self.timer = timer
        self.sleep = sleep
        self.store = S.WorkflowStore(conn, clock, user_id, self.tracer)
        self.gate = ApprovalGate(conn, clock, user_id)
        self.executor = Executor(conn, clock, env, self.gate, user_id, self.tracer,
                                 on_user_cancel=self.store.mark_user_cancelled, timer=timer, sleep=sleep)
        self.builder = ContextBuilder(memory, tracer=self.tracer)
        self.startup_messages: list[str] = []
        if recover:
            self.startup_messages = [o.message for o in self.executor.recover()]
            if self.startup_messages:
                wf = self.store.load()
                wf.state = S.IDLE  # recovery finalized the in-flight action
                self.store.save(wf)

    # ================================================================ public
    def handle_user_message(self, text: str) -> str:
        self._turn_usd = 0.0
        self._asked_provider = None
        notices = self.poll_and_handle_events()
        self.store.add_turn("user", text)
        self.tracer.event("user_message", text=text)
        try:
            reply = self._handle(text)
        except Exception as e:  # last-resort guard: never crash the chat, never claim success
            self.tracer.event("error", error=f"{type(e).__name__}: {e}")
            reply = "Sorry, something went wrong on my side, so I stopped. Nothing was booked, cancelled or changed."
        out = "\n\n".join([*notices, reply]) if notices else reply
        self.tracer.event("turn_cost", cost_usd=round(self._turn_usd, 6), cost_inr=usd_to_inr(self._turn_usd))
        self.store.add_turn("assistant", out)
        # bind approval to what the user has actually SEEN: the card active when this reply was shown
        active = self.gate.active()
        self.store.set_shown(active.action_id if active else None)
        return out

    def finish(self) -> str:
        """Emit the end-of-run summary event and return its one-line form."""
        if not hasattr(self.tracer, "run_summary"):
            return ""
        from .trace import summary_line
        return summary_line(self.tracer.run_summary(time.monotonic() - self._t_start))

    def poll_and_handle_events(self) -> list[str]:
        """Proactive follow-up, called between turns: provider cancellations -> notify -> replacement -> approval."""
        msgs = []
        for ev in self.env.poll_events():
            self.tracer.event("external_event", event=ev)
            if ev.get("type") != "provider_cancelled":
                continue
            bid, slot = ev["booking_id"], ev.get("slot", "")
            if self.store.user_cancelled(bid) or slot <= self.clock.now().isoformat(timespec="minutes"):
                self.tracer.event("event_ignored", booking_id=bid, reason="user-cancelled or past")
                continue
            pname = mock_data.BY_ID.get(ev.get("provider_id"), {}).get("name", ev.get("provider_id"))
            msg = f"Heads up: {pname} cancelled your {ev['service'].replace('_', ' ')} visit on {when(slot)} (booking {bid})."
            msgs.append(msg + " " + self._replacement(ev))
        return msgs

    def deliver_events(self) -> list[str]:
        """`/events`: poll, and bind the next 'yes' to the card these messages just showed the user."""
        msgs = self.poll_and_handle_events()
        if msgs:
            active = self.gate.active()
            self.store.set_shown(active.action_id if active else None)
        return msgs

    # ================================================================ core
    def _handle(self, text: str) -> str:
        wf = self.store.load()
        pending = self.gate.active()
        conf = wf.notes.pop("confirm", None) if self.memory is not None else None
        if conf:
            self.store.save(wf)
            if is_affirmative(text) or is_negative(text):
                yes = is_affirmative(text)
                try:
                    if yes:
                        self.memory.confirm(conf["id"])
                    else:
                        self.memory.forget(conf["id"])
                except Exception:
                    pass
                self.tracer.event("guess_answered", fact_id=conf["id"], remembered=yes)
                ack = "Okay, I'll remember that." if yes else "Okay, I've dropped that guess."
                return ack + "\n" + self._handle(conf["text"])
        if is_affirmative(text):
            if pending:
                shown = self.store.shown()
                if shown != pending.action_id:
                    # e.g. a proactive event superseded the card between the user's view and their "yes"
                    self.tracer.event("gate_refused", reason="approval not for the card last shown",
                                      shown=shown, pending=pending.action_id)
                    return ("Your 'yes' was for an earlier request that has since changed, so I did nothing. "
                            f"Here's the current one:\nPlease approve: {pending.card.friendly()}\n"
                            "Reply 'yes' to confirm or 'no' to cancel.")
                return self._approve(wf, shown)
            latest = self.gate.latest()
            if latest and latest.status == A_DONE:
                return f"That's already done, nothing more to do. ({latest.card.friendly()} I checked it earlier.)"
            return "There is nothing waiting for your approval right now."
        if pending and is_negative(text):
            self.gate.set_status(pending.action_id, DECLINED)
            self.store.transition(wf, S.GATHERING, "user declined")
            return "No problem, I won't do that. Nothing was booked or changed. Want me to look for something else?"

        r = route(text, self.clock.now(), wf.intent)
        self.tracer.event("route", intent=r.intent, slots=r.slots, correction=r.is_correction)
        if r.intent == "memory_command":
            return self._memory_command(r)
        if r.intent in ("provider_question", "compare_providers"):
            # read-only questions: never touch slots, the pending approval or the workflow state
            self._asked_provider = r.slots.get("provider_id")
            return self._loop(text, wf, r.intent)
        prev_intent = wf.intent
        if r.intent in ACTION_INTENTS:
            terminal = wf.state in (S.DONE, S.FAILED, S.ESCALATED, S.IDLE)
            if (r.intent != wf.intent and not r.is_correction) or (terminal and not r.is_correction):
                wf.slots, wf.notes = S.Slots(), {k: v for k, v in wf.notes.items() if k == "asked_guesses"}
            wf.intent = r.intent
        changed = wf.slots.merge(r.slots)
        if changed:
            self.tracer.event("slots_updated", changed=changed, slots=wf.slots.to_dict())
        if pending and (changed or (r.intent in ACTION_INTENTS and r.intent != prev_intent)):
            self.gate.invalidate_active("user changed request: " + ",".join(changed))
            self.tracer.event("approval_invalidated", action_id=pending.action_id, changed=changed)
        if wf.intent in ACTION_INTENTS:
            self._memory_defaults(wf)
        if wf.intent in ACTION_INTENTS and not pending and self.ask_guesses:
            q = self._guess_question(wf, text)
            if q:
                self.store.save(wf)
                return q
        self.store.save(wf)
        if wf.intent in ACTION_INTENTS:
            self.store.transition(wf, S.GATHERING if wf.missing() else S.SEARCHING, f"intent={wf.intent}")
        return self._loop(text, wf, r.intent)

    def _guess_question(self, wf: S.Workflow, text: str) -> str | None:
        """If an unconfirmed inferred guess bears on this request, ask once, plainly. Never used until confirmed."""
        if self.memory is None:
            return None
        sl = wf.slots
        mem = wf.notes.get("from_memory", {})  # only what the user said counts, not defaults from memory
        topics = {t for t in (sl.service, "time" if sl.earliest_time and "earliest_time" not in mem else None,
                              "price" if sl.budget_inr and "budget_inr" not in mem else None) if t}
        asked = wf.notes.setdefault("asked_guesses", [])
        try:
            cands = self.memory.guesses_to_confirm(topics, exclude=asked)
        except Exception:
            return None
        if not cands:
            return None
        f = cands[0]
        asked.append(f.id)
        wf.notes["confirm"] = {"id": f.id, "text": text}
        self.tracer.event("guess_question", fact_id=f.id, key=f.key)
        return f"{_guess_phrase(f.value)}. Should I remember that?"

    def _memory_defaults(self, wf: S.Workflow) -> None:
        if self.memory is None:
            return
        try:
            facts = self.memory.relevant(wf.intent, wf.slots.service, list(wf.slots.to_dict()), 20)
        except Exception:
            return
        filled = {}
        for f in facts:
            spec = MEMORY_SLOT_MAP.get(f.key)
            if not spec:
                continue
            slot, parser = spec
            if getattr(wf.slots, slot) is not None or slot in filled:
                continue
            v = parser(f.value)
            if v is not None:
                filled[slot] = v
                wf.notes.setdefault("from_memory", {})[slot] = f.id
        if filled:
            wf.slots.merge(filled)
            self.tracer.event("slots_from_memory", filled=filled)

    def _state_dict(self, wf: S.Workflow) -> dict:
        p = self.gate.active()
        d = {"user_id": self.user_id, "state": wf.state, "intent": wf.intent, "slots": wf.slots.to_dict(),
             "missing": wf.missing(), "today": self.clock.now().date().isoformat()}
        if getattr(self, "_asked_provider", None):
            d["asked_provider"] = self._asked_provider
        if wf.notes.get("from_memory"):
            d["slots_from_memory"] = sorted(wf.notes["from_memory"])
        if wf.notes.get("alternatives"):
            d["alternatives"] = wf.notes["alternatives"]
        if p:
            d["pending_approval"] = p.card.render()
        return d

    def _loop(self, text: str, wf: S.Workflow, turn_intent: str | None = None) -> str:
        history = self.store.turns(60)[:-1]  # exclude the just-added current user turn
        scratch: list[dict] = []
        t0, tool_calls = self.timer(), 0
        for turn in range(self.limits.max_model_turns):
            if self.timer() - t0 > self.limits.wall_clock_s:
                return self._exit("wall_clock", "That took too long, so I stopped. Nothing was booked or changed. "
                                  "Please ask again, maybe with fewer details.")
            st = self._state_dict(wf)
            st["turn_intent"] = turn_intent
            ctx = self.builder.build(st, history, text, scratch)
            self.tracer.context(ctx)
            self._context_stat(ctx)
            m0 = self.timer()
            reply = self.model.respond(ctx, AGENT_TOOLS)
            u = reply.usage or {}
            self._turn_usd += u["cost_usd"] if u.get("cost_usd") is not None else estimate_cost(
                u.get("model", "default"), u.get("input_tokens", 0), u.get("output_tokens", 0))
            self.tracer.model_call(u.get("model", getattr(self.model, "name", "model")), u.get("input_tokens", 0),
                                   u.get("output_tokens", 0), u.get("cost_usd"), int((self.timer() - m0) * 1000),
                                   context_fact_ids=ctx.fact_ids, context_hash=ctx.hash, context_tokens=ctx.tokens)
            if not reply.tool_calls:
                return reply.text or "Okay."
            proposed = None
            for call in reply.tool_calls:
                tool_calls += 1
                if tool_calls > self.limits.max_tool_calls:
                    return self._exit("max_tool_calls", "That needed more steps than I allow in one go. Nothing was "
                                      "booked or changed. Could you tell me the service, area and date?")
                content, raw, card = self._dispatch(call, wf)
                scratch.append({"kind": "call", "id": call.id, "name": call.name, "args": call.args})
                scratch.append({"kind": "result", "id": call.id, "name": call.name, "content": content, "raw": raw})
                if card:
                    proposed = card
            if proposed:
                lead = (reply.text.strip() + "\n") if reply.text.strip() else ""
                return lead + proposed
        return self._exit("max_model_turns", "I couldn't sort that out in one go. Nothing was booked or "
                          "changed. Could you rephrase, or give me a bit more detail?")

    def _context_stat(self, ctx) -> None:
        """One trace line per model turn: how much memory exists vs. was sent, and context size vs. the cap."""
        if self.memory is None:
            return
        try:
            facts = self.memory.all(include_forgotten=True)
        except Exception:
            return
        excluded = sum(1 for f in facts if f.forgotten or (f.source == "inferred" and not f.confirmed))
        self.tracer.event("context_stat", stored=len(facts), sent=len(ctx.fact_ids), excluded=excluded,
                          tokens=ctx.tokens, cap=self.builder.cap,
                          line=context_stat_line(len(facts), len(ctx.fact_ids), excluded, ctx.tokens, self.builder.cap))

    def _exit(self, reason: str, msg: str) -> str:
        self.tracer.event("limit_exit", reason=reason)
        return msg

    # ================================================================ tools
    def _dispatch(self, call: ToolCall, wf: S.Workflow) -> tuple[str, dict, str | None]:
        """Returns (content for the model, raw result, rendered approval card if a proposal was created)."""
        if call.name not in AGENT_TOOL_NAMES:
            self.tracer.event("blocked_tool", name=call.name, args=call.args)
            r = {"status": "error", "message": f"tool '{call.name}' is not available to the assistant; "
                                               "consequential actions require propose_* and user approval"}
            return json.dumps(r), r, None
        if call.name.startswith("propose_"):
            return self._propose(call, wf)
        args = {k: v for k, v in call.args.items() if k in ("service", "area", "date_range", "provider_id", "user_id",
                                                               "hold_id", "idempotency_key")}
        if call.name == "list_bookings":
            args = {"user_id": self.user_id}
        res = self._read(call.name, args)
        if call.name == "search_providers" and res.get("status") == "ok":
            self._rank_results(wf, res)
            self._remember_search(wf, res)
        if call.name == "search_providers" and wf.state in (S.SEARCHING, S.GATHERING):
            self.store.transition(wf, S.COMPARING, "search results")
        return wrap_untrusted(call.name, json.dumps(_compact(call.name, res), ensure_ascii=False)), res, None

    def _rank_results(self, wf: S.Workflow, res: dict) -> None:
        """Rank in shared code (any model): log the ranking trace and sort what the model sees the same way."""
        provs = res.get("providers") or []
        ranked = [p for p, _ in rank(provs, wf.slots.to_dict(), tracer=self.tracer)]
        ids = {p["provider_id"] for p in ranked}
        res["providers"] = ranked + [p for p in provs if p["provider_id"] not in ids]

    def _read(self, name: str, args: dict) -> dict:
        schema = _SCHEMAS.get(name)
        if schema is not None:  # validate model-supplied args instead of catching TypeError from the call
            props = schema["input_schema"].get("properties", {})
            args = {k: v for k, v in args.items() if k in props}
            missing = [k for k in schema["input_schema"].get("required", []) if k not in args]
            if missing:
                return {"status": "error", "message": f"{name}: missing required argument(s): {', '.join(missing)}"}
        for attempt in range(3):
            try:
                return call_tool(self.env, self.tracer, name, **args)
            except TransientError:
                if attempt < 2:
                    self.sleep(0.1 * 2 ** attempt)  # bounded exponential backoff
                continue
            except ToolTimeout:
                return {"status": "error", "message": f"{name} timed out"}
            except ToolError as e:
                return {"status": "error", "message": str(e)}
        return {"status": "error", "message": f"{name} unavailable (503)"}

    @staticmethod
    def _remember_search(wf: S.Workflow, res: dict) -> None:
        wf.notes["last_search"] = {p["provider_id"]: {"slots": list(p.get("available_slots", [])), "area": p.get("area"),
                                                      "service": p.get("service"), "price_inr": p.get("price_inr")}
                                   for p in res.get("providers", [])}

    def _check_proposal(self, wf: S.Workflow, pid: str, slot: str) -> tuple[str | None, list[str]]:
        """Validate a model-proposed booking against current search results and the user's constraints.
        Returns (refusal reason or None, warnings to show on the card)."""
        found = (wf.notes.get("last_search") or {}).get(pid)
        sl = wf.slots
        if found is None:
            return f"provider {pid} is not in the current search results; search first", []
        if slot not in found["slots"]:
            return f"slot {slot} is not an available slot for {pid} in the current search results", []
        if sl.service and found.get("service") and found["service"] != sl.service:
            return f"provider {pid} offers {found['service']}, not {sl.service}", []
        if sl.earliest_time and slot[11:16] < sl.earliest_time:
            return f"slot {slot} is earlier than the user's earliest time {sl.earliest_time}", []
        if sl.date_start and (slot[:10] < sl.date_start or slot[:10] > (sl.date_end or sl.date_start)):
            return f"slot {slot} is outside the requested dates {sl.date_start}..{sl.date_end or sl.date_start}", []
        warn = []
        if sl.area and found.get("area") and found["area"].lower() != sl.area.lower():
            warn.append(f"Warning: they're in {found['area']}, not {sl.area} where you asked.")
        return None, warn

    def _existing_booking(self, pid: str, slot: str, service: str | None) -> tuple[dict | None, list[dict]]:
        """(confirmed booking with same provider+slot, confirmed bookings of the same service on the same day)."""
        lb = self._read("list_bookings", {"user_id": self.user_id})
        conf = [b for b in lb.get("bookings", []) if b.get("status") == "confirmed"]
        same = next((b for b in conf if b["provider_id"] == pid and b["slot"] == slot), None)
        sameday = [b for b in conf if b["slot"][:10] == slot[:10] and service and b.get("service") == service]
        return same, sameday

    def _booking(self, booking_id: str) -> dict | None:
        lb = self._read("list_bookings", {"user_id": self.user_id})
        return next((b for b in lb.get("bookings", []) if b["booking_id"] == booking_id), None)

    def _propose(self, call: ToolCall, wf: S.Workflow) -> tuple[str, dict, str | None]:
        a = call.args
        extra = set(a) - {"provider_id", "slot", "booking_id"}
        if extra:  # e.g. {"approved": true} - the model cannot approve anything
            self.tracer.event("ignored_model_args", tool=call.name, args=sorted(extra))
        if call.name == "propose_booking":
            pid, slot = str(a.get("provider_id", "")), str(a.get("slot", ""))
            p = mock_data.BY_ID.get(pid)
            if not p:
                r = {"status": "error", "message": f"unknown provider {pid}"}
                return json.dumps(r), r, None
            why, warnings = self._check_proposal(wf, pid, slot)
            if why:
                self.tracer.event("proposal_refused", provider_id=pid, slot=slot, reason=why)
                r = {"status": "error", "message": "proposal refused: " + why}
                return json.dumps(r), r, None
            same, sameday = self._existing_booking(pid, slot, p["service"])
            if same:
                r = {"status": "error", "message": f"the user already has confirmed booking {same['booking_id']} with "
                                                   f"{pid} at {slot}; do not book it again"}
                return json.dumps(r), r, None
            for b in sameday:
                warnings.append(f"Note: you already have {b['service'].replace('_', ' ')} booked that day "
                                f"({b['booking_id']}, {b['provider_name']}, {when(b['slot'])}).")
            h = self._read("hold_slot", {"provider_id": pid, "slot": slot})
            if h.get("status") != "held":
                return json.dumps(h), h, None
            card = ActionCard("book", pid, p["name"], slot, h["quoted_price"], None, p["service"])
            note = ""
            if h["quoted_price"] != h["search_price"]:
                note += (f"\nHeads up: the price changed from {inr(h['search_price'])} to {inr(h['quoted_price'])} "
                         "once I held the slot.")
            if wf.slots.budget_inr and h["quoted_price"] > wf.slots.budget_inr:
                note += f"\nWarning: that's above your {inr(wf.slots.budget_inr)} budget."
            for w in warnings:
                note += "\n" + w
            act = self.gate.propose(card, h["hold_id"], h["expires_at"])
            note += self._hold_note(act)
        else:
            bid = str(a.get("booking_id", ""))
            b = self._booking(bid)
            if not b or b["status"] != "confirmed":
                r = {"status": "error", "message": f"booking {bid} not found or not active"}
                return json.dumps(r), r, None
            if call.name == "propose_cancel":
                card = ActionCard("cancel", b["provider_id"], b["provider_name"], b["slot"], b["price"], bid, b["service"])
            else:
                slot = str(a.get("slot", ""))
                card = ActionCard("reschedule", b["provider_id"], b["provider_name"], slot, b["price"], bid, b["service"])
            act = self.gate.propose(card)
            note = ""
        self.store.transition(wf, S.AWAITING_APPROVAL, f"proposed {card.type}")
        self.tracer.event("pending_action", action_id=act.action_id, hash=act.hash, fingerprint=card.fingerprint(), card_text=card.render())
        why, fact_ids = self._why(wf, card)
        self.tracer.event("proposal_why", action_id=act.action_id, fact_ids=fact_ids, why=why)
        rendered = f"Please approve: {card.friendly()} ({card.fingerprint()}){note}\n{why}\nReply 'yes' to confirm or 'no' to cancel."
        return rendered, {"status": "proposed", "action_id": act.action_id}, rendered

    def _why(self, wf: S.Workflow, card: ActionCard) -> tuple[str, list[str]]:
        """One-line 'Why: ...' citing the price/rating/fit facts and the user's stored preference facts that were used.
        Only explicit-or-confirmed, non-forgotten memory facts are ever cited (the ids go to the trace)."""
        ids: list[str] = []
        sl = wf.slots
        facts = {}
        if self.memory is not None:
            try:
                facts = {f.id: f for f in self.memory.all() if f.source == "explicit" or f.confirmed}
            except Exception:
                facts = {}
        from_mem = wf.notes.get("from_memory", {})

        def cite(fid) -> str:
            if fid in facts:
                ids.append(fid)
                return f" [{fid}]"
            return ""

        if card.type != "book":
            verb = "cancel" if card.type == "cancel" else "move"
            return f"Why: you asked me to {verb} booking {card.booking_id}.", ids
        p = mock_data.BY_ID.get(card.provider_id, {})
        parts = []
        price = inr(card.price_inr)
        if sl.budget_inr and card.price_inr is not None and card.price_inr <= sl.budget_inr:
            parts.append(f"{price} fits your {inr(sl.budget_inr)} budget" + cite(from_mem.get("budget_inr")))
        else:
            parts.append(price)
        if p.get("rating"):
            parts.append(f"rated {p['rating']}")
        if sl.area and p.get("area", "").lower() == sl.area.lower():
            parts.append(f"in {p['area']}" + cite(from_mem.get("area")))
        if sl.earliest_time and card.slot[11:16] >= sl.earliest_time:
            parts.append(f"after your {sl.earliest_time} earliest" + cite(from_mem.get("earliest_time")))
        pref = next((f for f in facts.values() if f.key.startswith("pref.") and card.provider_id in f.value), None)
        if pref:
            parts.append("you've liked them before" + cite(pref.id))
        if not (p.get("attributes") or {}).get("emergency") and "budget.dislikes_surge" in {f.key for f in facts.values()}:
            sf = next(f for f in facts.values() if f.key == "budget.dislikes_surge")
            parts.append("no surge pricing" + cite(sf.id))
        return "Why: " + ", ".join(parts) + ".", list(dict.fromkeys(i for i in ids if i))

    # ================================================================ approval + execution
    def _hold_note(self, act) -> str:
        n = act.hold_minutes_left(self.clock)
        return f"\nSlot held for {n} min." if n is not None else ""

    def _approve(self, wf: S.Workflow, action_id: str | None = None) -> str:
        try:
            action = self.gate.authorize(True, action_id)
        except GateError as e:
            self.tracer.event("gate_refused", reason=str(e))
            return f"I can't execute that ({e}). Nothing was done."
        self.tracer.event("approved", action_id=action.action_id, hash=action.hash)
        if wf.state != S.AWAITING_APPROVAL:
            self.store.transition(wf, S.AWAITING_APPROVAL, "pending action restored")
        self.store.transition(wf, S.EXECUTING, f"user approved {action.type}")
        out = self.executor.execute(action, deadline=self.timer() + self.limits.wall_clock_s)
        self.tracer.event("execution", status=out.status, result=out.result)
        if out.status == "done":
            self.store.transition(wf, S.DONE, out.message)
            return out.message
        if out.status == "reask" and out.new_card:
            self.tracer.event("approval_invalidated", action_id=action.action_id,
                              reason=f"quote changed {action.card.price_inr} -> {out.new_card.price_inr}; re-asking")
            new = self.gate.propose(out.new_card, out.hold["hold_id"], out.hold["expires_at"])
            self.store.transition(wf, S.AWAITING_APPROVAL, "re-ask after change")
            self.tracer.event("pending_action", action_id=new.action_id, hash=new.hash, fingerprint=new.card.fingerprint(), card_text=new.card.render())
            why, fact_ids = self._why(wf, new.card)
            self.tracer.event("proposal_why", action_id=new.action_id, fact_ids=fact_ids, why=why)
            return (f"{out.message}\nPlease approve: {new.card.friendly()} ({new.card.fingerprint()})"
                    f"{self._hold_note(new)}\n{why}\n"
                    "Reply 'yes' to confirm or 'no' to cancel.")
        if out.status == "changed":
            self.store.transition(wf, S.FAILED, "action target changed since approval")
            return out.message
        if out.status == "duplicate":
            self.store.transition(wf, S.DONE, "duplicate booking refused")
            return out.message
        if out.status == "conflict":
            wf.notes["alternatives"] = out.alternatives
            wf.slots.slot = None
            self.store.save(wf)
            self.store.transition(wf, S.COMPARING, "reschedule slot conflict")
            return out.message + " Which one works for you? I'll ask you to approve again before changing anything."
        if out.status == "unknown":
            self.store.transition(wf, S.ESCALATED, "outcome unknown")
            return out.message
        self.store.transition(wf, S.FAILED, out.message)
        return out.message

    # ================================================================ proactive replacement
    def _replacement(self, ev: dict) -> str:
        wf = S.Workflow(self.user_id, self.store.load().state, "new_booking")
        day = ev["slot"][:10]
        wf.slots.merge({"service": ev["service"], "date_start": day, "date_end": day})
        self._memory_defaults(wf)
        if wf.slots.area is None:
            wf.slots.area = mock_data.BY_ID.get(ev.get("provider_id"), {}).get("area")
        end = (date.fromisoformat(day) + timedelta(days=2)).isoformat()
        res = self._read("search_providers", {"service": ev["service"], "area": wf.slots.area,
                                               "date_range": {"start": day, "end": end}})
        self._remember_search(wf, res)
        cands = [p for p in res.get("providers", []) if p["provider_id"] != ev.get("provider_id")]
        ranked = rank(cands, {"budget_inr": wf.slots.budget_inr, "earliest_time": wf.slots.earliest_time},
                      require_verified=True, tracer=self.tracer)
        if not ranked:
            return "I couldn't find a replacement that fits your preferences. Want me to widen the search?"
        p, slot = ranked[0]
        wf.slots.date_end = end
        current = self.store.load()
        current.intent, current.slots, current.notes = "new_booking", wf.slots, {"replacing": ev["booking_id"],
                                                                                 "last_search": wf.notes.get("last_search", {}),
                                                                                 **({"from_memory": wf.notes["from_memory"]} if wf.notes.get("from_memory") else {})}
        self.store.save(current)
        content, _, card = self._propose(ToolCall("propose_booking", {"provider_id": p["provider_id"], "slot": slot}), current)
        if not card:
            return "I found a replacement but couldn't hold the slot. Want me to search again?"
        return "I found a replacement that fits your preferences. " + card

    # ================================================================ memory commands
    def _memory_command(self, r) -> str:
        if self.memory is None:
            return "Memory isn't enabled in this session."
        if r.memory_op == "show":
            facts = self.memory.all()
            if not facts:
                return "I don't have anything stored about you yet."
            told = [f for f in facts if f.source == "explicit"]
            confirmed = [f for f in facts if f.source == "inferred" and f.confirmed]
            guessed = [f for f in facts if f.source == "inferred" and not f.confirmed]
            line = lambda f: f"- [{f.id}] {f.value}"
            out = ["Here's what I know about you."]
            if told:
                out += ["", "You told me:", *map(line, told)]
            if confirmed:
                out += ["", "I picked these up and you confirmed them:", *map(line, confirmed)]
            if guessed:
                out += ["", "My guesses (I won't use these until you confirm):", *map(line, guessed)]
            out += ["", "Say 'forget <id or topic>' to remove anything."]
            return "\n".join(out)
        if r.memory_op == "forget":
            arg = (r.memory_arg or "").strip()
            target = self.memory.get(arg)
            if target is None:
                words = [w for w in arg.replace(".", " ").split() if len(w) > 2]
                hits = [f for f in self.memory.all() if words and all(w in (f.key + " " + f.value).lower() for w in words)]
                target = hits[0] if len(hits) == 1 else None
                if target is None and hits:
                    return "That matches several memories: " + ", ".join(f"[{f.id}] {f.key}" for f in hits[:6]) + \
                           ". Which one should I forget?"
            if target is None or not self.memory.forget(target.id):
                return f"I couldn't find anything about '{arg}' in what I know."
            return f"Done, I've forgotten [{target.id}] {target.key}. I won't use it again."
        if r.memory_op == "remember":
            arg = (r.memory_arg or "").strip()
            key = "note." + "_".join(arg.split()[:4]).lower().strip(".,")
            f = self.memory.remember(key, arg, source="explicit", category="note", tags=["global", "new_booking"])
            return f"Got it, I'll remember: {f.value} [{f.id}]."
        return "Sorry, I didn't understand that memory command."


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:8]
