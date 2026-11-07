"""Model interface + adapters.

``Model.respond(context, tools) -> ModelReply(text, tool_calls, usage)``.

* ScriptedModel: deterministic (scripted replies and/or a heuristic policy) for offline tests/evals;
  its tool calls go through exactly the same agent loop as a live model's.
* ClaudeAgentModel: claude-agent-sdk (auth via CLAUDE_CODE_OAUTH_TOKEN, Claude Pro plan). The SDK's
  own tools are disabled; the model answers in a JSON envelope that we parse into tool calls.
* AnthropicAPIModel: native tool use via the anthropic SDK when ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .mocks import TOOL_SCHEMAS


@dataclass
class ToolCall:
    name: str
    args: dict
    id: str = field(default_factory=lambda: "tc_" + uuid.uuid4().hex[:8])


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # model, input_tokens, output_tokens, cost_usd


@dataclass
class Context:
    system: str
    messages: list[dict]          # [{"role": "user"|"assistant", "content": str}], last one is the current user turn
    scratch: list[dict]           # this turn: {"kind":"call",id,name,args} / {"kind":"result",id,name,content,raw}
    state: dict                   # structured workflow state (intent, slots, missing, pending)
    fact_ids: list[str] = field(default_factory=list)
    hash: str = ""
    tokens: int = 0

    def render(self) -> str:
        """Single-string rendering (used by the JSON-envelope adapter)."""
        parts = [f"{m['role'].upper()}: {m['content']}" for m in self.messages]
        for s in self.scratch:
            if s["kind"] == "call":
                parts.append(f"ASSISTANT TOOL CALL {s['id']}: {s['name']}({json.dumps(s['args'])})")
            else:
                parts.append(f"TOOL RESULT {s['id']} ({s['name']}): {s['content']}")
        return "\n".join(parts)


class Model:
    name = "model"

    def respond(self, context: Context, tools: list[dict]) -> ModelReply:
        raise NotImplementedError


# ------------------------------------------------------------------ tools the model may call
READ_ONLY_TOOLS = ("search_providers", "get_provider", "list_bookings", "get_booking_status")
_S = {"type": "string"}


def _t(name, desc, props, req):
    return {"name": name, "description": desc, "input_schema": {"type": "object", "properties": props, "required": req}}


PROPOSAL_TOOLS = [
    _t("propose_booking", "Propose booking a provider's slot. Code places a hold and shows the user an approval card. "
       "This does NOT book; only the user can approve.", {"provider_id": _S, "slot": _S}, ["provider_id", "slot"]),
    _t("propose_cancel", "Propose cancelling one of the user's bookings. The user must approve the card.",
       {"booking_id": _S}, ["booking_id"]),
    _t("propose_reschedule", "Propose moving a booking to a new slot. The user must approve the card.",
       {"booking_id": _S, "slot": _S}, ["booking_id", "slot"]),
]
AGENT_TOOLS = [t for t in TOOL_SCHEMAS if t["name"] in READ_ONLY_TOOLS] + PROPOSAL_TOOLS
AGENT_TOOL_NAMES = {t["name"] for t in AGENT_TOOLS}
# consequential tools are deliberately absent from AGENT_TOOLS
assert not AGENT_TOOL_NAMES & {"book", "cancel", "reschedule", "hold_slot"}


# ------------------------------------------------------------------ scripted
def _results(ctx: Context, name: str) -> list[dict]:
    return [s["raw"] for s in ctx.scratch if s["kind"] == "result" and s["name"] == name and isinstance(s.get("raw"), dict)]


def _slot_ok(slot: str, sl: dict) -> bool:
    d, t = slot.split("T")
    if sl.get("date_start") and d < sl["date_start"]:
        return False
    if sl.get("date_end") and d > sl["date_end"]:
        return False
    if sl.get("earliest_time") and t < sl["earliest_time"]:
        return False
    return True


def heuristic_policy(ctx: Context, tools: list[dict]) -> ModelReply:
    """Deterministic stand-in for an LLM: drives real tool calls for each intent."""
    st, sl = ctx.state, ctx.state.get("slots", {})
    intent = st.get("intent")
    if any(s["kind"] == "result" and s["name"].startswith("propose_") for s in ctx.scratch):
        last = [s for s in ctx.scratch if s["kind"] == "result"][-1]
        return ModelReply(text=last["content"] if last["name"].startswith("propose_") else "Here is what I found.")
    if st.get("turn_intent") in ("smalltalk", None) and st.get("pending_approval"):
        return ModelReply(text="Still waiting for your decision on: " + st["pending_approval"] + " (reply yes or no).")
    if st.get("turn_intent") == "smalltalk":
        intent = "smalltalk"
    if st.get("missing") and intent != "smalltalk":
        return ModelReply(text="Could you tell me the " + ", ".join(m.replace("date_start", "date") for m in st["missing"]) + "?")
    if intent in ("new_booking", "rebook_same_provider"):
        want_pid = sl.get("provider_id")
        if intent == "rebook_same_provider" and not want_pid:
            lb = _results(ctx, "list_bookings")
            if not lb:
                return ModelReply(tool_calls=[ToolCall("list_bookings", {"user_id": st.get("user_id", "u_demo")})])
            past = [b for b in lb[-1]["bookings"] if b["service"] == sl.get("service") and b["status"] in ("completed", "confirmed")]
            if not past:
                return ModelReply(text=f"I couldn't find a previous {sl.get('service')} booking.")
            want_pid = sorted(past, key=lambda b: b["slot"])[-1]["provider_id"]
        sr = _results(ctx, "search_providers")
        if not sr:
            dr = {"start": sl.get("date_start"), "end": sl.get("date_end") or sl.get("date_start")} if sl.get("date_start") else None
            return ModelReply(tool_calls=[ToolCall("search_providers", {"service": sl.get("service"), "area": sl.get("area") or "",
                                                                         "date_range": dr})])
        cands = []
        for p in sr[-1].get("providers", []):
            if want_pid and p["provider_id"] != want_pid:
                continue
            if sl.get("budget_inr") and p["price_inr"] > sl["budget_inr"]:
                continue
            ok = [s for s in p["available_slots"] if _slot_ok(s, sl)]
            if ok:
                cands.append((not p.get("in_requested_area", True) if not want_pid else False, -p["rating"], p["price_inr"], ok[0], p))
        if not cands:
            return ModelReply(text="I couldn't find a provider matching all your constraints. Want me to relax the budget, time or dates?")
        cands.sort(key=lambda c: c[:3])
        _, _, _, slot, p = cands[0]
        return ModelReply(text=f"Best option: {p['name']}.", tool_calls=[ToolCall("propose_booking", {"provider_id": p["provider_id"], "slot": slot})])
    if intent in ("cancel", "reschedule"):
        lb = _results(ctx, "list_bookings")
        if not lb:
            return ModelReply(tool_calls=[ToolCall("list_bookings", {"user_id": st.get("user_id", "u_demo")})])
        ups = [b for b in lb[-1]["bookings"] if b["status"] == "confirmed"
               and (not sl.get("booking_id") or b["booking_id"] == sl["booking_id"])
               and (not sl.get("service") or b["service"] == sl["service"])]
        if not ups:
            return ModelReply(text="I couldn't find a matching upcoming booking.")
        b = sorted(ups, key=lambda b: b["slot"])[0]
        if intent == "cancel":
            return ModelReply(tool_calls=[ToolCall("propose_cancel", {"booking_id": b["booking_id"]})])
        target = sl.get("slot")
        if not target and sl.get("date_start"):
            hh = sl.get("earliest_time") or b["slot"].split("T")[1]
            target = f"{sl['date_start']}T{hh}"
        if not target:
            return ModelReply(text=f"When would you like to move booking {b['booking_id']} ({b['slot']}) to?")
        return ModelReply(tool_calls=[ToolCall("propose_reschedule", {"booking_id": b["booking_id"], "slot": target})])
    return ModelReply(text="Hi! I can book, reschedule or cancel plumbers, electricians and AC servicing. What do you need?")


class ScriptedModel(Model):
    """Deterministic model. ``script`` items are ModelReply or callables (ctx, tools) -> ModelReply, consumed in
    order; when exhausted, ``policy`` (default heuristic_policy) answers. Records every context it saw."""
    name = "scripted"

    def __init__(self, script: list[ModelReply | Callable] | None = None,
                 policy: Callable[[Context, list[dict]], ModelReply] | None = heuristic_policy):
        self.script = list(script or [])
        self.policy = policy
        self.seen: list[Context] = []

    def respond(self, context: Context, tools: list[dict]) -> ModelReply:
        self.seen.append(context)
        if self.script:
            item = self.script.pop(0)
            r = item(context, tools) if callable(item) else item
        elif self.policy:
            r = self.policy(context, tools)
        else:
            r = ModelReply(text="(no scripted reply)")
        if not r.usage:
            r.usage = {"model": self.name, "input_tokens": context.tokens, "output_tokens": len(r.text) // 4 + 1,
                       "cost_usd": 0.0}
        return r


# ------------------------------------------------------------------ live adapters
ENVELOPE_INSTRUCTIONS = """
You act only by replying with ONE JSON object and nothing else:
{"text": "<message to the user, may be empty>", "tool_calls": [{"name": "<tool>", "args": {...}}]}
Available tools (JSON schema):
%s
Call tools when you need data; when you have tool results, answer or propose. Use an empty tool_calls list to finish.
"""


def parse_envelope(raw: str) -> tuple[str, list[ToolCall]]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return (raw or "").strip(), []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (raw or "").strip(), []
    calls = [ToolCall(c.get("name", ""), c.get("args") or {}) for c in obj.get("tool_calls") or [] if isinstance(c, dict)]
    return str(obj.get("text") or ""), calls


class ClaudeAgentModel(Model):
    """claude-agent-sdk adapter (uses CLAUDE_CODE_OAUTH_TOKEN; Sonnet by default; single turn, no built-in tools)."""

    def __init__(self, model: str | None = None):
        self.name = model or os.environ.get("ERRAND_MODEL", "claude-sonnet-4-5")

    def respond(self, context: Context, tools: list[dict]) -> ModelReply:
        import anyio
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

        system = context.system + ENVELOPE_INSTRUCTIONS % json.dumps(tools, separators=(",", ":"))
        opts = ClaudeAgentOptions(system_prompt=system, model=self.name, max_turns=1, tools=[], allowed_tools=[],
                                  setting_sources=[])
        out: dict[str, Any] = {"text": "", "usage": {}, "cost": None}

        async def run():
            async for msg in query(prompt=context.render(), options=opts):
                if isinstance(msg, AssistantMessage):
                    out["text"] += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
                elif isinstance(msg, ResultMessage):
                    out["usage"], out["cost"] = msg.usage or {}, msg.total_cost_usd
                    if not out["text"] and msg.result:
                        out["text"] = msg.result

        anyio.run(run)
        text, calls = parse_envelope(out["text"])
        u = out["usage"]
        return ModelReply(text, calls, {"model": self.name, "input_tokens": u.get("input_tokens", 0),
                                        "output_tokens": u.get("output_tokens", 0), "cost_usd": out["cost"]})


class AnthropicAPIModel(Model):
    """Native tool use via the anthropic SDK (only if ANTHROPIC_API_KEY is set)."""

    def __init__(self, model: str | None = None, max_tokens: int = 800):
        import anthropic
        self.client = anthropic.Anthropic()
        self.name = model or os.environ.get("ERRAND_MODEL", "claude-sonnet-4-5")
        self.max_tokens = max_tokens

    def respond(self, context: Context, tools: list[dict]) -> ModelReply:
        msgs = [{"role": m["role"], "content": m["content"]} for m in context.messages]
        for s in context.scratch:
            if s["kind"] == "call":
                msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": s["id"], "name": s["name"], "input": s["args"]}]})
            else:
                msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": s["id"], "content": s["content"]}]})
        merged: list[dict] = []  # merge consecutive same-role messages (API requires alternation)
        for m in msgs:
            if merged and merged[-1]["role"] == m["role"]:
                a, b = merged[-1]["content"], m["content"]
                a = a if isinstance(a, list) else [{"type": "text", "text": a}]
                b = b if isinstance(b, list) else [{"type": "text", "text": b}]
                merged[-1]["content"] = a + b
            else:
                merged.append(dict(m))
        r = self.client.messages.create(model=self.name, max_tokens=self.max_tokens, system=context.system,
                                        tools=tools, messages=merged)
        text = "".join(b.text for b in r.content if b.type == "text")
        calls = [ToolCall(b.name, dict(b.input), b.id) for b in r.content if b.type == "tool_use"]
        return ModelReply(text, calls, {"model": self.name, "input_tokens": r.usage.input_tokens,
                                        "output_tokens": r.usage.output_tokens, "cost_usd": None})


def default_model() -> Model:
    if os.environ.get("ERRAND_MODEL_KIND") == "scripted":
        return ScriptedModel()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicAPIModel()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ClaudeAgentModel()
    return ScriptedModel()
