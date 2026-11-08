"""CLI: `python -m errand_agent [--scripted] [--scenario NAME] [--reset] [--plain] [--db state.db]`.

Default is a two-pane rich UI (chat | live trace). `--plain` prints chat to stdout and writes the
trace to runs/<run_id>.jsonl (`tail -f` it). Slash commands: /events, /cancel-provider <booking_id>,
/memory, /forget <key>, /quit. Debug: /crash-mid-booking.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import db
from .agent import Agent, new_run_id
from .clock import RealClock
from .memory import MemoryStore, seed_memories
from .mocks import MockEnvironment, Scenario
from .models import ScriptedModel, default_model
from .trace import Tracer

SCENARIOS = {
    "happy": "first booking succeeds",
    "timeout_booked": "book call times out but the booking actually went through",
    "timeout_not_booked": "book call times out and nothing was booked; the retry succeeds",
    "slot_taken": "first booking attempt loses the slot",
    "hold_expired": "slot hold expires before booking",
    "price_drift": "quoted price rises above the search price (needs re-approval)",
    "slot_conflict": "first reschedule hits a slot conflict",
    "transient_error": "first booking call fails with a 503",
    "demo": "recording run: injection review on, re-hold price drifts, 1st book times out but books",
    "injection": "default catalogue with prompt-injection reviews (no forced outcome)",
}


def make_scenario(name: str | None, seed: int = 42) -> Scenario:
    sc = Scenario(seed=seed)
    if name in (None, "", "injection"):
        return sc
    if name == "demo":
        # until the first booking exists: holds last 1 min, so approving triggers a re-hold that quotes +150
        sc.first_booking_hold_minutes, sc.first_booking_rehold_drift = 1, 150
        return sc.force("book", 1, "timeout_booked").force("book", 2, "success").force("book", 3, "success")
    if name == "happy":
        return sc.force("book", 1, "success")
    if name == "price_drift":
        sc.quote_drift = 150
        return sc.force("book", 1, "success")
    if name == "slot_conflict":
        return sc.force("book", 1, "success").force("reschedule", 1, "slot_conflict")
    if name == "timeout_not_booked":
        return sc.force("book", 1, "timeout_not_booked").force("book", 2, "success")
    if name in ("timeout_booked", "slot_taken", "hold_expired", "transient_error"):
        return sc.force("book", 1, name)
    raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader; never overrides real env vars and never prints values."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        os.environ.setdefault(k, v.strip().strip("'\""))


def reset_db(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        Path(db_path + suffix).unlink(missing_ok=True)


def build(db_path: str, scripted: bool, seed: int = 42, scenario: str | None = None,
          tracer: Tracer | None = None) -> Agent:
    conn = db.connect(db_path)
    clock = RealClock()
    env = MockEnvironment(conn=conn, clock=clock, scenario=make_scenario(scenario, seed))
    mem = MemoryStore(conn, clock, "u_demo")
    if not mem.all(include_forgotten=True):
        seed_memories(mem)
    model = ScriptedModel() if scripted else default_model()
    return Agent(conn, env, model, clock, memory=mem, tracer=tracer or Tracer(new_run_id()), ask_guesses=True)


def slash_to_message(text: str) -> str | None:
    """Map memory slash commands onto natural-language messages the router understands."""
    if text == "/memory":
        return "what do you remember about me"
    if text.startswith("/forget "):
        return "forget " + text.split(None, 1)[1]
    return None


def arm_crash(agent: Agent, kill=None) -> None:
    """DEMO/DEBUG aid (`/crash-mid-booking`): on the next `book` call the booking reaches the provider, then the
    process is SIGKILL-style terminated before the agent sees the reply. Restart without --reset to watch recovery
    re-verify the same idempotency key. `kill` is injectable for tests."""
    real = agent.env.book

    def book_then_die(*a, **kw):
        try:
            real(*a, **kw)
        finally:
            msg = "[debug] booking reached the provider; killing the process before the reply was processed"
            print(msg, file=sys.stderr, flush=True)
            (kill or (lambda: os._exit(137)))()
        return real(*a, **kw)  # only reached when `kill` returns (tests)

    agent.env.book = book_then_die


def run_plain(agent: Agent) -> None:
    print(f"Errand agent ({agent.model.name}). Trace: {agent.tracer.path}  (tail -f it). /quit to exit.")
    for m in agent.startup_messages:
        print("agent (recovery)>", m)
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/events":
            for m in agent.deliver_events() or ["(no events)"]:
                print("agent>", m)
            continue
        if text == "/crash-mid-booking":
            arm_crash(agent)
            print("sys> armed: the next booking call will reach the provider, then the process is killed")
            continue
        if text.startswith(("/cancel-provider ", "/cancel-by-provider ")):
            try:
                ev = agent.env.emit_provider_cancellation(text.split()[1])
                agent.tracer.event("external_event", event=ev, note="simulated provider cancellation")
                print(f"sys> simulated: provider cancelled {ev['booking_id']}. Run /events or send any message.")
            except (KeyError, IndexError):
                print("unknown booking id")
            continue
        print("agent>", agent.handle_user_message(slash_to_message(text) or text))
    print(agent.finish())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="errand_agent")
    ap.add_argument("--db", default=os.environ.get("ERRAND_DB", "state.db"))
    ap.add_argument("--scripted", action="store_true", help="offline deterministic model")
    ap.add_argument("--scenario", choices=list(SCENARIOS), help="force mock outcomes for the demo")
    ap.add_argument("--reset", action="store_true", help="delete and reseed the database first")
    ap.add_argument("--plain", action="store_true", help="plain stdout chat; trace only in runs/*.jsonl")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)
    load_dotenv()
    if not a.scripted and not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        sys.exit("No CLAUDE_CODE_OAUTH_TOKEN (env or .env) found for the live model; use --scripted for offline.")
    if a.reset:
        reset_db(a.db)
    if a.plain:
        run_plain(build(a.db, a.scripted, a.seed, a.scenario))
    else:
        from .ui import run_ui
        run_ui(a.db, a.scripted, a.seed, a.scenario)


if __name__ == "__main__":
    main()
