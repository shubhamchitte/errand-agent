"""Minimal CLI: `python -m errand_agent [--db state.db] [--scripted]`.

Commands: /events (poll provider events), /cancel-by-provider <booking_id> (simulate), /quit.
"""
from __future__ import annotations

import argparse
import os

from . import db
from .agent import Agent, new_run_id
from .clock import RealClock
from .memory import MemoryStore, seed_memories
from .mocks import MockEnvironment, Scenario
from .models import ScriptedModel, default_model
from .trace import Tracer


def build(db_path: str, scripted: bool, seed: int = 42) -> Agent:
    conn = db.connect(db_path)
    clock = RealClock()
    env = MockEnvironment(conn=conn, clock=clock, scenario=Scenario(seed=seed))
    mem = MemoryStore(conn, clock, "u_demo")
    if not mem.all(include_forgotten=True):
        seed_memories(mem)
    model = ScriptedModel() if scripted else default_model()
    return Agent(conn, env, model, clock, memory=mem, tracer=Tracer(new_run_id()))


def main() -> None:
    ap = argparse.ArgumentParser(prog="errand_agent")
    ap.add_argument("--db", default=os.environ.get("ERRAND_DB", "state.db"))
    ap.add_argument("--scripted", action="store_true", help="offline deterministic model")
    a = ap.parse_args()
    agent = build(a.db, a.scripted)
    print(f"Errand agent ({agent.model.name}). Type /quit to exit.")
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
            for m in agent.poll_and_handle_events() or ["(no events)"]:
                print("agent>", m)
            continue
        if text.startswith("/cancel-by-provider "):
            print(agent.env.emit_provider_cancellation(text.split()[1]))
            continue
        print("agent>", agent.handle_user_message(text))


if __name__ == "__main__":
    main()
