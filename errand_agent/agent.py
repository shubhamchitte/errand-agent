"""Agent loop: router -> slots -> bounded model/tool loop -> approval gate -> executor.

The model only sees read-only tools and propose_* tools. Approval comes exclusively from the
user's own message text (``is_affirmative``) and is checked by ApprovalGate against the latest
pending action's hash. Consequential tools are called only by Executor.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from . import state as S
from .approval import DONE as A_DONE, ActionCard, ApprovalGate, DECLINED, GateError
from .context import ContextBuilder, wrap_untrusted
from .executor import Executor, call_tool
from .mocks import ToolError, ToolTimeout, TransientError
from .mocks import data as mock_data
from .models import AGENT_TOOL_NAMES, AGENT_TOOLS, Model, ToolCall
from .router import extract_slots, is_affirmative, is_negative, route

ACTION_INTENTS = ("new_booking", "reschedule", "cancel", "rebook_same_provider")
# memory keys whose values seed empty slots (documented, deterministic)
MEMORY_SLOT_KEYS = {"earliest": "earliest_time", "budget.typical": "budget_inr", "area.locality": "area"}


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


class Agent:
    def __init__(self, conn, env, model: Model, clock, user_id: str = "u_demo", memory=None, tracer=None,
                 limits: Limits | None = None, timer=time.monotonic, recover: bool = True):
        self.conn, self.env, self.model, self.clock, self.user_id = conn, env, model, clock, user_id
        self.memory = memory
        self.tracer = tracer if tracer is not None else _NullTracer()
        self.limits = limits or Limits()
        self.timer = timer
        self.store = S.WorkflowStore(conn, clock, user_id, self.tracer)
        self.gate = ApprovalGate(conn, clock, user_id)
        self.executor = Executor(conn, clock, env, self.gate, user_id, self.tracer,
                                 on_user_cancel=self.store.mark_user_cancelled)
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
        notices = self.poll_and_handle_events()
        self.store.add_turn("user", text)
        self.tracer.event("user_message", text=text)
        try:
            reply = self._handle(text)
        except Exception as e:  # last-resort guard: never crash the chat, never claim success
            self.tracer.event("error", error=f"{type(e).__name__}: {e}")
            reply = "Something went wrong on my side and I stopped. Nothing new was booked, cancelled or changed."
        out = "\n\n".join([*notices, reply]) if notices else reply
        self.store.add_turn("assistant", out)
        return out

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
            msg = f"Heads up: {pname} cancelled your {ev['service'].replace('_', ' ')} booking {bid} on {slot}."
            msgs.append(msg + " " + self._replacement(ev))
        return msgs

    # ================================================================ core
    def _handle(self, text: str) -> str:
        wf = self.store.load()
        pending = self.gate.active()
        if is_affirmative(text):
            if pending:
                return self._approve(wf)
            latest = self.gate.latest()
            if latest and latest.status == A_DONE:
                return f"That's already done - nothing more to do. ({latest.card.render()}; result verified earlier.)"
            return "There is nothing waiting for your approval right now."
        if pending and is_negative(text):
            self.gate.set_status(pending.action_id, DECLINED)
            self.store.transition(wf, S.GATHERING, "user declined")
            return "Okay, I won't do that. Nothing was booked or changed. Want me to look for something else?"

        r = route(text, self.clock.now(), wf.intent)
        self.tracer.event("route", intent=r.intent, slots=r.slots, correction=r.is_correction)
        if r.intent == "memory_command":
            return self._memory_command(r)
        prev_intent = wf.intent
        if r.intent in ACTION_INTENTS:
            terminal = wf.state in (S.DONE, S.FAILED, S.ESCALATED, S.IDLE)
            if (r.intent != wf.intent and not r.is_correction) or (terminal and not r.is_correction):
                wf.slots, wf.notes = S.Slots(), {}
            wf.intent = r.intent
        changed = wf.slots.merge(r.slots)
        if changed:
            self.tracer.event("slots_updated", changed=changed, slots=wf.slots.to_dict())
        if pending and (changed or (r.intent in ACTION_INTENTS and r.intent != prev_intent)):
            self.gate.invalidate_active("user changed request: " + ",".join(changed))
            self.tracer.event("approval_invalidated", action_id=pending.action_id, changed=changed)
        if wf.intent in ACTION_INTENTS:
            self._memory_defaults(wf)
        self.store.save(wf)
        if wf.intent in ACTION_INTENTS:
            self.store.transition(wf, S.GATHERING if wf.missing() else S.SEARCHING, f"intent={wf.intent}")
        return self._loop(text, wf, r.intent)

    def _memory_defaults(self, wf: S.Workflow) -> None:
        if self.memory is None:
            return
        try:
            facts = self.memory.relevant(wf.intent, wf.slots.service, list(wf.slots.to_dict()), 20)
        except Exception:
            return
        filled = {}
        for f in facts:
            for prefix, slot in MEMORY_SLOT_KEYS.items():
                if prefix in f.key and getattr(wf.slots, slot) is None and slot not in filled:
                    v = extract_slots(f.value.replace("Rs", "rs"), self.clock.now()).get(slot)
                    if slot == "budget_inr" and v is None:
                        import re
                        m = re.search(r"(\d{3,6})", f.value)
                        v = int(m.group(1)) if m else None
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
                return self._exit("wall_clock", "I ran out of time working on this. Nothing was booked or changed; "
                                  "please ask again or narrow the request.")
            st = self._state_dict(wf)
            st["turn_intent"] = turn_intent
            ctx = self.builder.build(st, history, text, scratch)
            m0 = self.timer()
            reply = self.model.respond(ctx, AGENT_TOOLS)
            u = reply.usage or {}
            self.tracer.model_call(u.get("model", getattr(self.model, "name", "model")), u.get("input_tokens", 0),
                                   u.get("output_tokens", 0), u.get("cost_usd"), int((self.timer() - m0) * 1000),
                                   context_fact_ids=ctx.fact_ids, context_hash=ctx.hash, context_tokens=ctx.tokens)
            if not reply.tool_calls:
                return reply.text or "Okay."
            proposed = None
            for call in reply.tool_calls:
                tool_calls += 1
                if tool_calls > self.limits.max_tool_calls:
                    return self._exit("max_tool_calls", "I hit my limit on tool calls for one request. Nothing was "
                                      "booked or changed; could you narrow it down (service, area, date)?")
                content, raw, card = self._dispatch(call, wf)
                scratch.append({"kind": "call", "id": call.id, "name": call.name, "args": call.args})
                scratch.append({"kind": "result", "id": call.id, "name": call.name, "content": content, "raw": raw})
                if card:
                    proposed = card
            if proposed:
                lead = (reply.text.strip() + "\n") if reply.text.strip() else ""
                return lead + proposed
        return self._exit("max_model_turns", "I couldn't finish this within my step limit. Nothing was booked or "
                          "changed; please try rephrasing or give me more specifics.")

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
        if call.name == "search_providers" and wf.state in (S.SEARCHING, S.GATHERING):
            self.store.transition(wf, S.COMPARING, "search results")
        return wrap_untrusted(call.name, json.dumps(_compact(call.name, res), ensure_ascii=False)), res, None

    def _read(self, name: str, args: dict) -> dict:
        for attempt in range(3):
            try:
                return call_tool(self.env, self.tracer, name, **args)
            except TransientError:
                continue
            except ToolTimeout:
                return {"status": "error", "message": f"{name} timed out"}
            except (ToolError, TypeError) as e:
                return {"status": "error", "message": str(e)}
        return {"status": "error", "message": f"{name} unavailable (503)"}

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
            h = self._read("hold_slot", {"provider_id": pid, "slot": slot})
            if h.get("status") != "held":
                return json.dumps(h), h, None
            card = ActionCard("book", pid, p["name"], slot, h["quoted_price"], None, p["service"])
            note = ""
            if h["quoted_price"] != h["search_price"]:
                note += (f"\nNote: the quoted price changed from ₹{h['search_price']} (search) to ₹{h['quoted_price']} "
                         "when I held the slot.")
            if wf.slots.budget_inr and h["quoted_price"] > wf.slots.budget_inr:
                note += f"\nWarning: this is above your budget of ₹{wf.slots.budget_inr}."
            act = self.gate.propose(card, h["hold_id"], h["expires_at"])
            note += f"\n(Slot held until {h['expires_at'][11:16]}.)"
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
        self.tracer.event("pending_action", action_id=act.action_id, hash=act.hash, card=card.render())
        rendered = f"Please approve: {card.render()}{note}\nReply 'yes' to confirm or 'no' to cancel."
        return rendered, {"status": "proposed", "action_id": act.action_id}, rendered

    # ================================================================ approval + execution
    def _approve(self, wf: S.Workflow) -> str:
        try:
            action = self.gate.authorize(True)
        except GateError as e:
            self.tracer.event("gate_refused", reason=str(e))
            return f"I can't execute that ({e}). Nothing was done."
        self.tracer.event("approved", action_id=action.action_id, hash=action.hash)
        if wf.state != S.AWAITING_APPROVAL:
            self.store.transition(wf, S.AWAITING_APPROVAL, "pending action restored")
        self.store.transition(wf, S.EXECUTING, f"user approved {action.type}")
        out = self.executor.execute(action)
        self.tracer.event("execution", status=out.status, result=out.result)
        if out.status == "done":
            self.store.transition(wf, S.DONE, out.message)
            return out.message
        if out.status == "reask" and out.new_card:
            new = self.gate.propose(out.new_card, out.hold["hold_id"], out.hold["expires_at"])
            self.store.transition(wf, S.AWAITING_APPROVAL, "re-ask after change")
            self.tracer.event("pending_action", action_id=new.action_id, hash=new.hash, card=new.card.render())
            return f"{out.message}\nPlease approve: {new.card.render()}\nReply 'yes' to confirm or 'no' to cancel."
        if out.status == "conflict":
            wf.notes["alternatives"] = out.alternatives
            wf.slots.slot = None
            self.store.save(wf)
            self.store.transition(wf, S.COMPARING, "reschedule slot conflict")
            return out.message + " Which one would you like? I'll ask for your approval again before changing anything."
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
        from datetime import date, timedelta
        end = (date.fromisoformat(day) + timedelta(days=2)).isoformat()
        res = self._read("search_providers", {"service": ev["service"], "area": wf.slots.area,
                                               "date_range": {"start": day, "end": end}})
        best = None
        for p in res.get("providers", []):
            if p["provider_id"] == ev.get("provider_id") or not (p.get("attributes") or {}).get("verified", True):
                continue
            if wf.slots.budget_inr and p["price_inr"] > wf.slots.budget_inr:
                continue
            ok = [s for s in p["available_slots"] if not wf.slots.earliest_time or s[11:16] >= wf.slots.earliest_time]
            if ok:
                k = (not p.get("in_requested_area"), -p["rating"], p["price_inr"])
                if best is None or k < best[0]:
                    best = (k, p, ok[0])
        if best is None:
            return "I couldn't find a replacement within your preferences; tell me if I should widen the search."
        _, p, slot = best
        wf.slots.date_end = end
        current = self.store.load()
        current.intent, current.slots, current.notes = "new_booking", wf.slots, {"replacing": ev["booking_id"],
                                                                                 **({"from_memory": wf.notes["from_memory"]} if wf.notes.get("from_memory") else {})}
        self.store.save(current)
        content, _, card = self._propose(ToolCall("propose_booking", {"provider_id": p["provider_id"], "slot": slot}), current)
        if not card:
            return "I found a possible replacement but couldn't hold the slot; want me to search again?"
        return "I found a replacement within your preferences. " + card

    # ================================================================ memory commands
    def _memory_command(self, r) -> str:
        if self.memory is None:
            return "Memory isn't enabled in this session."
        if r.memory_op == "show":
            facts = self.memory.all()
            if not facts:
                return "I don't have anything stored about you."
            lines = [f"- [{f.id}] {f.key}: {f.value}" + ("" if f.source == "explicit" or f.confirmed else " (inferred, unconfirmed)")
                     for f in facts]
            return "Here's what I remember:\n" + "\n".join(lines)
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
                return f"I couldn't find a memory matching '{arg}'."
            return f"Done - I've forgotten [{target.id}] {target.key}. I won't use it again."
        if r.memory_op == "remember":
            arg = (r.memory_arg or "").strip()
            key = "note." + "_".join(arg.split()[:4]).lower().strip(".,")
            f = self.memory.remember(key, arg, source="explicit", category="note", tags=["global", "new_booking"])
            return f"Got it, I'll remember: {f.value} [{f.id}]."
        return "Sorry, I didn't understand that memory command."


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:8]
