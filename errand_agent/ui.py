"""Two-pane rich demo UI: chat (left) | live trace (right), prompt line at the bottom.

The agent core is untouched: `UITracer` subclasses `Tracer` and mirrors every event it writes to the
JSONL file into an in-memory list that the right pane renders.
"""
from __future__ import annotations

import json
import re
import textwrap
import threading
import time
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from .agent import Agent, new_run_id
from .text import INJECTION_RE
from .trace import Tracer

HELP = ("/events  poll provider events\n/crash-mid-booking  DEBUG: kill the process during the next booking\n/cancel-provider <booking_id>  simulate provider cancellation\n"
        "/memory  /forget <key>  /help  /quit")


class UITracer(Tracer):
    def __init__(self, run_id: str, path_dir: str = "runs/"):
        super().__init__(run_id, path_dir)
        self.lock = threading.Lock()

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self._events)


def _short(v: Any, n: int = 110) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _t(ts: str) -> str:
    return ts[11:19] if len(ts) >= 19 else ""


def trace_items(events: list[dict], width: int) -> list[tuple[Any, int]]:
    """Convert events to (renderable, line_count) pairs. Lines are pre-truncated to `width`."""
    out: list[tuple[Any, int]] = []

    def line(style: str, s: str, extra: str = "") -> None:
        txt = Text(_short(s, width), style=style, no_wrap=True, overflow="ellipsis")
        out.append((txt, 1))

    for e in events:
        t, ts = e["type"], _t(e.get("ts", ""))
        if t == "state_transition":
            line("bold cyan", f"{ts} STATE {e['frm']} -> {e['to']}" + (f"  ({e['reason']})" if e.get("reason") else ""))
        elif t == "model_call":
            line("magenta", f"{ts} MODEL {e['model']} in={e['input_tokens']} out={e['output_tokens']} "
                            f"${e['cost_usd']:.4f} (₹{e.get('cost_inr', 0):.2f}) {e['latency_ms']:.0f}ms")
        elif t == "context_stat":
            line("bold white on grey23", e["line"])
        elif t == "tool_call":
            err = e.get("error")
            line("red" if err else "green",
                 f"{ts} TOOL {e['name']}({_short(e['args'], 60)}) {e['latency_ms']:.0f}ms" + (f" ERR {err}" if err else ""))
            line("dim", "    -> " + _short(e["result_sanitized"], width - 8))
            blob = json.dumps([e["args"], e["result_sanitized"]], default=str)
            if INJECTION_RE.search(blob):
                out.append((Text(_short("  !! INJECTION FLAG: untrusted text contains instructions - treated as data, ignored", width),
                                 style="bold black on yellow", no_wrap=True), 1))
        elif t == "pending_action":
            card = [ln[: width - 4] for ln in str(e.get("card", "")).splitlines()][:10]
            body = Text("\n".join(card), style="bold")
            out.append((Panel(body, title="APPROVAL CARD " + str(e.get("fingerprint", "")), border_style="bold yellow", style="on grey15", padding=(0, 1)),
                        len(card) + 2))
        elif t == "approval_check":
            line("bold green" if e.get("match") else "bold red",
                 f"{ts} APPROVAL CHECK {e['fingerprint']} match={e['match']}")
        elif t == "ranking":
            line("cyan", f"{ts} RANKING {', '.join(e['order'])} (review text used: {e['uses_review_text']})")
        elif t == "run_summary":
            line("bold magenta", f"{ts} {e.get('line', '')}")
        elif t == "turn_cost":
            line("magenta", f"{ts} TURN COST ${e['cost_usd']:.4f} (₹{e['cost_inr']:.2f})")
        elif t == "approved":
            line("bold green", f"{ts} APPROVED {_short({k: v for k, v in e.items() if k not in ('ts', 'run_id', 'type')}, 80)}")
        elif t in ("error", "gate_refused", "proposal_refused", "approval_invalidated", "approval_stale", "blocked_tool",
                   "duplicate_refused", "deadline_exceeded", "limit_exit"):
            line("bold red", f"{ts} {t.upper()} {_short({k: v for k, v in e.items() if k not in ('ts', 'run_id', 'type')}, 90)}")
        elif t in ("user_message", "slots_updated", "context", "idempotency_key_persisted"):
            continue
        else:
            line("yellow" if t in ("retry", "external_event", "recovery", "execution") else "dim",
                 f"{ts} {t} {_short({k: v for k, v in e.items() if k not in ('ts', 'run_id', 'type')}, 90)}")
    return out


def tail_fit(items: list[tuple[Any, int]], height: int) -> list[Any]:
    chosen, used = [], 0
    for r, n in reversed(items):
        if used + n > height:
            break
        chosen.append(r)
        used += n
    return list(reversed(chosen))


_MD = [(re.compile(r"\*\*(.+?)\*\*"), r"\1"), (re.compile(r"__(.+?)__"), r"\1"), (re.compile(r"`([^`]+)`"), r"\1"),
       (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""), (re.compile(r"^(\s*)[*-]\s+", re.M), r"\1- ")]


def strip_md(text: str) -> str:
    """The chat pane is plain wrapped Text; drop markdown emphasis/headings so `**bold**` never shows raw."""
    for rx, rep in _MD:
        text = rx.sub(rep, text)
    return text


def chat_lines(msgs: list[tuple[str, str]], width: int, height: int) -> list[Text]:
    lines: list[Text] = []
    for who, text in msgs:
        style = {"you": "bold blue", "agent": "white", "sys": "dim italic"}.get(who, "white")
        label = {"you": "you> ", "agent": "agent> ", "sys": "* "}.get(who, "")
        first = True
        for para in (strip_md(str(text)) if who == "agent" else str(text)).splitlines() or [""]:
            for w in textwrap.wrap(para, max(width - len(label), 10)) or [""]:
                lines.append(Text((label if first else " " * len(label)) + w, style=style))
                first = False
        lines.append(Text(""))
    return lines[-height:] if height > 0 else []


def render(console: Console, agent: Agent, msgs: list[tuple[str, str]], busy: bool = False) -> Layout:
    W, H = console.size
    H = max(H - 2, 10)  # leave room for the prompt line
    lw = W * 5 // 11
    rw = W - lw
    inner_h = H - 2
    s = agent.tracer.summary() if hasattr(agent.tracer, "summary") else {}
    events = agent.tracer.snapshot() if hasattr(agent.tracer, "snapshot") else []
    left = Panel(Group(*chat_lines(msgs, lw - 4, inner_h)), title=f"Chat - {agent.model.name}" + (" (thinking...)" if busy else ""),
                 border_style="blue", height=H)
    stats = (f"calls={s.get('model_calls', 0)} tok={s.get('input_tokens', 0)}/{s.get('output_tokens', 0)} "
             f"${s.get('cost_usd', 0):.4f}/₹{s.get('cost_inr', 0):.2f} tools={s.get('tool_calls', 0)} err={s.get('tool_errors', 0)}")
    right = Panel(Group(*tail_fit(trace_items(events, rw - 4), inner_h)), title=f"Trace - {stats}",
                  border_style="magenta", height=H)
    lay = Layout()
    lay.split_row(Layout(left, size=lw), Layout(right, size=rw))
    return lay


def run_ui(db_path: str, scripted: bool, seed: int = 42, scenario: str | None = None,
           console: Console | None = None) -> None:
    from .__main__ import build, slash_to_message

    console = console or Console()
    tracer = UITracer(new_run_id())
    agent = build(db_path, scripted, seed, scenario, tracer=tracer)
    msgs: list[tuple[str, str]] = [("sys", f"scenario={scenario or 'default'}  trace file: {tracer.path}  /help for commands")]
    msgs += [("agent", f"(recovery) {m}") for m in agent.startup_messages]
    # alternate screen: one frame visible at a time, nothing stacked into the scrollback
    with console.screen():
        _loop(console, agent, msgs, slash_to_message)
    console.print(agent.finish())


def _loop(console: Console, agent: Agent, msgs: list[tuple[str, str]], slash_to_message) -> None:
    from rich.live import Live

    while True:
        console.clear()
        console.print(render(console, agent, msgs), end="")
        try:
            text = console.input("[bold blue]you> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        msgs.append(("you", text))
        if text == "/help":
            msgs.append(("sys", HELP))
            continue
        job = _job(agent, text, slash_to_message)
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["out"] = job()
            except Exception as e:  # never crash the demo
                box["out"] = f"[ui error] {type(e).__name__}: {e}"

        th = threading.Thread(target=work, daemon=True)
        th.start()
        console.clear()
        with Live(render(console, agent, msgs, True), console=console, refresh_per_second=8, auto_refresh=False,
                  transient=True) as live:
            while th.is_alive():
                live.update(render(console, agent, msgs, True), refresh=True)
                time.sleep(0.12)
        out = box.get("out")
        for who, m in (out if isinstance(out, list) else [("agent", out)]):
            msgs.append((who, m))


def _job(agent: Agent, text: str, slash_to_message):
    def events() -> list[tuple[str, str]]:
        return [("agent", m) for m in (agent.deliver_events() or ["(no events)"])]

    if text == "/events":
        return events
    if text == "/crash-mid-booking":
        from .__main__ import arm_crash
        arm_crash(agent)
        return lambda: [("sys", "armed: the next booking call will reach the provider, then the process is killed")]
    if text.startswith(("/cancel-provider ", "/cancel-by-provider ")):
        def cancel():
            try:
                ev = agent.env.emit_provider_cancellation(text.split()[1])
            except (KeyError, IndexError):
                return [("sys", "unknown booking id")]
            agent.tracer.event("external_event", event=ev, note="simulated provider cancellation")
            return [("sys", f"simulated: provider cancelled {ev['booking_id']}. Run /events or send any message.")]
        return cancel
    return lambda: [("agent", agent.handle_user_message(slash_to_message(text) or text))]
