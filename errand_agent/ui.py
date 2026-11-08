"""Two-pane rich demo UI: chat (left) | live trace (right), prompt line at the bottom.

The agent core is untouched: `UITracer` subclasses `Tracer` and mirrors every event it writes to the
JSONL file into an in-memory list that the right pane renders.
"""
from __future__ import annotations

import json
import os
import re
import textwrap
import threading
import time
from typing import Any

try:  # line editing + history at the prompt (arrow keys instead of ^[[A)
    import readline  # noqa: F401
except ImportError:  # e.g. Windows
    readline = None

from rich.console import Console, Group
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from .agent import Agent, new_run_id
from .text import INJECTION_RE
from .trace import Tracer

HELP = ("/events  poll provider events\n/crash-mid-booking  DEBUG: kill the process during the next booking\n/cancel-provider <booking_id>  simulate provider cancellation\n"
        "/memory  /forget <key>  /help  /quit\n"
        "Scrolling: /up /down (page), /scroll N (+N lines up, -N down), /bottom (live view)\n"
        "/trace [N]  last N trace events as full JSON (default 5)   /chat  full chat history from the top\n"
        "Tip: to see anything cut off: /trace 5, or open runs/<id>.jsonl.")


class View:
    """Scroll/mode state. offset = lines scrolled up from the live bottom (0 = live)."""

    def __init__(self) -> None:
        self.offset = 0
        self.trace_n: int | None = None  # None = normal trace view, else show last N events as full JSON
        self.page = 10


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


def _wrap(s: str, width: int, indent: str = "    ") -> list[str]:
    return textwrap.wrap(s, max(width, 10), subsequent_indent=indent, break_long_words=True,
                         replace_whitespace=False) or [""]


def _t(ts: str) -> str:
    return ts[11:19] if len(ts) >= 19 else ""


def trace_items(events: list[dict], width: int) -> list[tuple[Any, int]]:
    """Convert events to (renderable, line_count) pairs. Lines are pre-truncated to `width`."""
    out: list[tuple[Any, int]] = []

    def line(style: str, s: str, extra: str = "") -> None:
        for chunk in _wrap(s, width):
            out.append((Text(chunk, style=style, no_wrap=True), 1))

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
                 f"{ts} TOOL {e['name']}({_short(e['args'], 400)}) {e['latency_ms']:.0f}ms" + (f" ERR {err}" if err else ""))
            line("dim", "    -> " + _short(e["result_sanitized"], 2000))
            blob = json.dumps([e["args"], e["result_sanitized"]], default=str)
            if INJECTION_RE.search(blob):
                for chunk in _wrap("  !! INJECTION FLAG: untrusted text contains instructions - treated as data, ignored", width):
                    out.append((Text(chunk, style="bold black on yellow", no_wrap=True), 1))
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
            line("bold green", f"{ts} APPROVED {_short({k: v for k, v in e.items() if k not in ('ts', 'run_id', 'type')}, 600)}")
        elif t in ("error", "gate_refused", "proposal_refused", "approval_invalidated", "approval_stale", "blocked_tool",
                   "duplicate_refused", "deadline_exceeded", "limit_exit"):
            line("bold red", f"{ts} {t.upper()} {_short({k: v for k, v in e.items() if k not in ('ts', 'run_id', 'type')}, 800)}")
        elif t in ("user_message", "slots_updated", "context", "idempotency_key_persisted"):
            continue
        else:
            line("yellow" if t in ("retry", "external_event", "recovery", "execution") else "dim",
                 f"{ts} {t} {_short({k: v for k, v in e.items() if k not in ('ts', 'run_id', 'type')}, 800)}")
    return out


def tail_fit(items: list[tuple[Any, int]], height: int, offset: int = 0) -> list[Any]:
    """Last `height` lines of items, optionally scrolled `offset` lines up from the end."""
    skipped = 0
    while offset > 0 and items and skipped + items[-1][1] <= offset:
        skipped += items[-1][1]
        items = items[:-1]
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


def chat_lines(msgs: list[tuple[str, str]], width: int, height: int, offset: int = 0) -> list[Text]:
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
    if height <= 0:
        return []
    offset = max(0, min(offset, max(len(lines) - height, 0)))
    end = len(lines) - offset
    return lines[max(end - height, 0):end]


def full_trace_items(events: list[dict], n: int, width: int) -> list[tuple[Any, int]]:
    out: list[tuple[Any, int]] = []
    for e in events[-max(n, 1):]:
        for chunk in _wrap(json.dumps(e, ensure_ascii=False, default=str), width, indent="  "):
            out.append((Text(chunk, style="white", no_wrap=True), 1))
        out.append((Text(""), 1))
    return out


def render(console: Console, agent: Agent, msgs: list[tuple[str, str]], busy: bool = False,
           view: "View | None" = None) -> Layout:
    view = view or View()
    W, H = console.size
    H = max(H - 2, 10)  # leave room for the prompt line
    lw = W * 5 // 11
    rw = W - lw
    inner_h = H - 2
    s = agent.tracer.summary() if hasattr(agent.tracer, "summary") else {}
    events = agent.tracer.snapshot() if hasattr(agent.tracer, "snapshot") else []
    view.page = max(inner_h - 2, 3)
    items = (full_trace_items(events, view.trace_n, rw - 4) if view.trace_n is not None
             else trace_items(events, rw - 4))
    longest = max(len(chat_lines(msgs, lw - 4, 10 ** 9)), sum(n for _, n in items))
    view.offset = max(0, min(view.offset, longest - 1))
    scrolled = f" [scrolled up {view.offset}, /bottom]" if view.offset else ""
    left = Panel(Group(*chat_lines(msgs, lw - 4, inner_h, view.offset)),
                 title=f"Chat - {agent.model.name}" + (" (thinking...)" if busy else "") + scrolled,
                 border_style="blue", height=H)
    stats = (f"calls={s.get('model_calls', 0)} tok={s.get('input_tokens', 0)}/{s.get('output_tokens', 0)} "
             f"${s.get('cost_usd', 0):.4f}/₹{s.get('cost_inr', 0):.2f} tools={s.get('tool_calls', 0)} err={s.get('tool_errors', 0)}")
    right = Panel(Group(*tail_fit(items, inner_h, view.offset)),
                  title=(f"Trace (last {view.trace_n} as JSON) - " if view.trace_n is not None else "Trace - ") + stats,
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
    _print_exit(console, tracer, msgs)


def transcript(msgs: list[tuple[str, str]]) -> str:
    label = {"you": "you> ", "agent": "agent> ", "sys": "* "}
    return "\n\n".join(label.get(w, "") + str(t) for w, t in msgs) + "\n"


def _print_exit(console: Console, tracer: Tracer, msgs: list[tuple[str, str]]) -> None:
    text = transcript(msgs)
    chat_path = os.path.splitext(str(tracer.path))[0] + ".chat.txt"
    try:
        with open(chat_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        chat_path = "(could not write)"
    console.print(f"\nTrace (JSONL): {tracer.path}\nChat transcript: {chat_path}\n", markup=False, highlight=False)
    console.print(text, markup=False, highlight=False)


def handle_view_command(view: View, text: str) -> str | None:
    """Scroll/mode commands. Returns a status message, or None if `text` is not a view command."""
    cmd, _, arg = text.partition(" ")
    arg = arg.strip()
    if cmd == "/up":
        view.offset += view.page
    elif cmd == "/down":
        view.offset = max(view.offset - view.page, 0)
    elif cmd == "/scroll":
        try:
            view.offset = max(view.offset + int(arg), 0)
        except ValueError:
            return "usage: /scroll N  (positive = up, negative = down)"
    elif cmd == "/bottom":
        view.offset, view.trace_n = 0, None
    elif cmd == "/trace":
        try:
            view.trace_n = int(arg) if arg else 5
        except ValueError:
            return "usage: /trace [N]"
        view.offset = 0
    elif cmd == "/chat":
        view.offset = 10 ** 9  # clamped to the top of the history on render
    else:
        return None
    return ""


def _loop(console: Console, agent: Agent, msgs: list[tuple[str, str]], slash_to_message) -> None:
    from rich.live import Live

    view = View()

    while True:
        console.clear()
        console.print(render(console, agent, msgs, view=view), end="")
        try:
            text = console.input("[bold blue]you> [/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        if text.split()[0] in ("/up", "/down", "/scroll", "/bottom", "/trace", "/chat"):
            note = handle_view_command(view, text)
            if note:
                msgs.append(("sys", note))
            continue
        msgs.append(("you", text))
        view.offset = 0
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
        with Live(render(console, agent, msgs, True, view), console=console, refresh_per_second=8, auto_refresh=False,
                  transient=True) as live:
            while th.is_alive():
                live.update(render(console, agent, msgs, True, view), refresh=True)
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
