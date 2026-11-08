"""JSONL run tracer: one JSON event per line under runs/<run_id>.jsonl."""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# USD per 1M tokens (input, output). Constant table; unknown models use "default".
PRICES: dict[str, tuple[float, float]] = {
    "scripted": (0.0, 0.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.8, 4.0),
    "claude-opus": (15.0, 75.0),
    "default": (3.0, 15.0),
}
# Fixed, documented conversion for display only (not a live FX rate): 1 USD = 84 INR.
USD_TO_INR = 84.0
MAX_STR = 200
_SECRET_KEY = re.compile(
    r"(password|passwd|secret|api[_-]?key|authorization|gate_code|(^|[_-])(token|otp|pin|card(_number)?)$)", re.I)
_SECRET_VAL = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})")


def sanitize(obj: Any) -> Any:
    """Recursively truncate long strings and redact secret-looking keys/values."""
    if isinstance(obj, dict):
        return {str(k): ("[REDACTED]" if _SECRET_KEY.search(str(k)) else sanitize(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str):
        s = _SECRET_VAL.sub("[REDACTED]", obj)
        return s if len(s) <= MAX_STR else s[:MAX_STR] + f"...[+{len(s) - MAX_STR} chars]"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return sanitize(str(obj))


def usd_to_inr(usd: float) -> float:
    return round(usd * USD_TO_INR, 4)


def estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    p = PRICES.get(model)
    if p is None:
        p = next((v for k, v in PRICES.items() if k != "default" and model.startswith(k)), PRICES["default"])
    return round((in_tok * p[0] + out_tok * p[1]) / 1_000_000, 6)


class Tracer:
    def __init__(self, run_id: str, path_dir: str | Path = "runs/"):
        self.run_id = run_id
        self.dir = Path(path_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.jsonl"
        self._events: list[dict] = []

    def event(self, type: str, **data: Any) -> dict:
        raw = data.pop("card_text", None)  # approval card text: shown in full (the generic sanitizer would cut it at 200 chars)
        ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
              "run_id": self.run_id, "type": type, **sanitize(data)}
        if raw is not None:
            ev["card_text"] = _SECRET_VAL.sub("[REDACTED]", str(raw))
        self._events.append(ev)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, sort_keys=False) + "\n")
        return ev

    def context(self, ctx) -> Path:
        """Write the FULL rendered model input for one model call to a sidecar JSONL
        (``<run_id>.context.jsonl``), unsanitized and untruncated, so evals can assert on the actual payload."""
        p = self.dir / f"{self.run_id}.context.jsonl"
        rec = {"run_id": self.run_id, "hash": ctx.hash, "tokens": ctx.tokens, "fact_ids": list(ctx.fact_ids),
               "system": _SECRET_VAL.sub("[REDACTED]", ctx.system),
               "rendered": _SECRET_VAL.sub("[REDACTED]", ctx.render())}
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return p

    def state_transition(self, frm: str, to: str, reason: str = "") -> dict:
        return self.event("state_transition", frm=frm, to=to, reason=reason)

    def model_call(self, model: str, input_tokens: int, output_tokens: int, cost_usd: float | None = None,
                   latency_ms: float = 0, context_fact_ids=(), context_hash: str = "", context_tokens: int = 0) -> dict:
        if cost_usd is None:
            cost_usd = estimate_cost(model, input_tokens, output_tokens)
        return self.event("model_call", model=model, input_tokens=input_tokens, output_tokens=output_tokens,
                          cost_usd=cost_usd, cost_inr=usd_to_inr(cost_usd), latency_ms=latency_ms, context_fact_ids=list(context_fact_ids),
                          context_hash=context_hash, context_tokens=context_tokens)

    def tool_call(self, name: str, args: Any, result_sanitized: Any, latency_ms: float = 0, error: str | None = None) -> dict:
        return self.event("tool_call", name=name, args=args, result_sanitized=result_sanitized,
                          latency_ms=latency_ms, error=error)

    def run_summary(self, elapsed_s: float = 0.0) -> dict:
        s = self.summary()
        ev = self.event("run_summary", ai_calls=s["model_calls"], tool_calls=s["tool_calls"],
                        tokens=s["input_tokens"] + s["output_tokens"], cost_inr=s["cost_inr"],
                        seconds=round(elapsed_s, 1))
        ev["line"] = summary_line(ev)
        return ev

    def summary(self) -> dict:
        mc = [e for e in self._events if e["type"] == "model_call"]
        tc = [e for e in self._events if e["type"] == "tool_call"]
        return {
            "run_id": self.run_id,
            "events": len(self._events),
            "event_counts": dict(Counter(e["type"] for e in self._events)),
            "model_calls": len(mc),
            "input_tokens": sum(e["input_tokens"] for e in mc),
            "output_tokens": sum(e["output_tokens"] for e in mc),
            "cost_usd": round(sum(e["cost_usd"] for e in mc), 6),
            "cost_inr": usd_to_inr(sum(e["cost_usd"] for e in mc)),
            "model_latency_ms": sum(e["latency_ms"] for e in mc),
            "tool_calls": len(tc),
            "tool_errors": sum(1 for e in tc if e.get("error")),
            "tool_latency_ms": sum(e["latency_ms"] for e in tc),
            "state_transitions": sum(1 for e in self._events if e["type"] == "state_transition"),
        }


def summary_line(ev: dict) -> str:
    return (f"{ev['ai_calls']} AI calls, {ev['tool_calls']} tool calls, {ev['tokens']} tokens, "
            f"\u20b9{ev['cost_inr']:.2f}, {ev['seconds']:.1f} s")


def context_stat_line(stored: int, sent: int, excluded: int, tokens: int, cap: int) -> str:
    return (f"memory: {stored} stored \u2192 {sent} sent ({excluded} excluded: forgotten/unconfirmed) "
            f"\u00b7 context {tokens:,}/{cap:,} tokens")
