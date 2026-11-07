"""Model interface + adapters.

``Model.respond(context, tools) -> ModelReply(text, tool_calls, usage)``.

* ScriptedModel: deterministic (scripted replies and/or a heuristic policy) for offline tests/evals;
  its tool calls go through exactly the same agent loop as a live model's.
* ClaudeAgentModel: claude-agent-sdk (auth via CLAUDE_CODE_OAUTH_TOKEN, Claude Pro plan). The SDK's
  own tools are disabled; our tools are native SDK MCP tools whose tool_use blocks become ToolCalls.
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
from .ranking import rank


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


def heuristic_policy(ctx: Context, tools: list[dict], tracer=None) -> ModelReply:
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
        provs = [p for p in sr[-1].get("providers", []) if not want_pid or p["provider_id"] == want_pid]
        if want_pid:  # asking for a specific provider: area fit is irrelevant
            provs = [{**p, "in_requested_area": True} for p in provs]
        cands = rank(provs, sl, tracer=tracer)
        if not cands:
            return ModelReply(text="I couldn't find a provider matching all your constraints. Want me to relax the budget, time or dates?")
        p, slot = cands[0]
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
        self.tracer = None  # set by Agent; lets the policy log its ranking

    def respond(self, context: Context, tools: list[dict]) -> ModelReply:
        self.seen.append(context)
        if self.script:
            item = self.script.pop(0)
            r = item(context, tools) if callable(item) else item
        elif self.policy:
            r = (self.policy(context, tools, tracer=self.tracer) if self.policy is heuristic_policy
                 else self.policy(context, tools))
        else:
            r = ModelReply(text="(no scripted reply)")
        if not r.usage:
            r.usage = {"model": self.name, "input_tokens": context.tokens, "output_tokens": len(r.text) // 4 + 1,
                       "cost_usd": 0.0}
        return r


# ------------------------------------------------------------------ live adapters
MCP_SERVER = "errand"
MCP_PREFIX = f"mcp__{MCP_SERVER}__"
_DEFERRED = ("Tool call recorded. The errand agent executes it (with approval gating) and returns the result in "
             "the next request; stop here.")


def parse_envelope(raw: str) -> tuple[str, list[ToolCall]]:
    """Legacy text-envelope parser (kept only as a fallback for plain-text replies; not used for tool calls)."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return (raw or "").strip(), []
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return (raw or "").strip(), []
    calls = [ToolCall(c.get("name", ""), c.get("args") or {}) for c in obj.get("tool_calls") or [] if isinstance(c, dict)]
    return str(obj.get("text") or ""), calls


def model_cost(model: str, input_tokens: int, output_tokens: int, reported: float | None = None) -> float:
    """Cost in USD: the provider-reported figure when present, else the PRICES table estimate."""
    from .trace import estimate_cost
    if reported is not None:
        return float(reported)
    return estimate_cost(model, int(input_tokens or 0), int(output_tokens or 0))


class ClaudeAgentModel(Model):
    """claude-agent-sdk adapter (auth via CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY; Sonnet by default).

    Uses NATIVE tool calls: our agent tools are exposed as in-process SDK MCP tools
    (``create_sdk_mcp_server``); the SDK's built-in tools are disabled (``tools=[]``) and only our MCP tools
    are allowed. ``max_turns=1`` bounds each call to one model step: tool_use blocks are captured and returned as
    ToolCalls so the agent loop (limits, approval gate, tracing) executes them - the MCP handler never touches
    the environment.
    """

    def __init__(self, model: str | None = None, max_turns: int = 1):
        self.name = model or os.environ.get("ERRAND_MODEL", "claude-sonnet-4-5")
        self.max_turns = max_turns

    def build_options(self, context: Context, tools: list[dict]):
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server
        from claude_agent_sdk import SdkMcpTool

        async def _deferred(_args):
            return {"content": [{"type": "text", "text": _DEFERRED}]}

        sdk_tools = [SdkMcpTool(t["name"], t["description"], t["input_schema"], _deferred) for t in tools]
        server = create_sdk_mcp_server(MCP_SERVER, tools=sdk_tools)
        return ClaudeAgentOptions(system_prompt=context.system, model=self.name, max_turns=self.max_turns, tools=[],
                                  allowed_tools=[MCP_PREFIX + t["name"] for t in tools],
                                  mcp_servers={MCP_SERVER: server}, strict_mcp_config=True, setting_sources=[])

    def collect(self, messages) -> ModelReply:
        """Turn SDK messages into a ModelReply (pure; unit-tested with constructed SDK message objects)."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
        text, calls, usage, reported = "", [], {}, None
        for msg in messages:
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock):
                        text += b.text
                    elif isinstance(b, ToolUseBlock):
                        name = b.name[len(MCP_PREFIX):] if b.name.startswith(MCP_PREFIX) else b.name
                        calls.append(ToolCall(name, dict(b.input or {}), b.id))
            elif isinstance(msg, ResultMessage):
                usage, reported = msg.usage or {}, msg.total_cost_usd
                if not text and not calls and msg.result:
                    text = msg.result
        it = int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0) \
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        ot = int(usage.get("output_tokens", 0) or 0)
        return ModelReply(text.strip(), calls, {"model": self.name, "input_tokens": it, "output_tokens": ot,
                                                "cost_usd": model_cost(self.name, it, ot, reported)})

    def respond(self, context: Context, tools: list[dict]) -> ModelReply:
        import anyio
        from claude_agent_sdk import query

        opts = self.build_options(context, tools)
        msgs: list = []

        async def run():
            async for msg in query(prompt=context.render(), options=opts):
                msgs.append(msg)

        anyio.run(run)
        return self.collect(msgs)


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
                                        "output_tokens": r.usage.output_tokens,
                                        "cost_usd": model_cost(self.name, r.usage.input_tokens, r.usage.output_tokens)})


def default_model() -> Model:
    if os.environ.get("ERRAND_MODEL_KIND") == "scripted":
        return ScriptedModel()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicAPIModel()
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ClaudeAgentModel()
    return ScriptedModel()
