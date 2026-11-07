"""Run one full booking where `book` times out although the booking really happened, and save the raw trace.

    .venv/bin/python scripts/make_e2e_trace.py   ->   traces/e2e_timeout_after_success.jsonl

Real agent loop, scripted model, forced mock outcome, fake clock. Flow: request -> search -> hold -> approval card ->
'yes' -> book (timeout, but booked) -> get_booking_status verifies -> single booking -> a repeated 'yes' is a no-op.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from errand_agent import db  # noqa: E402
from errand_agent.agent import Agent  # noqa: E402
from errand_agent.clock import FakeClock  # noqa: E402
from errand_agent.memory import MemoryStore, seed_memories  # noqa: E402
from errand_agent.mocks import MockEnvironment, Scenario  # noqa: E402
from errand_agent.models import ScriptedModel  # noqa: E402
from errand_agent.trace import Tracer  # noqa: E402

RUN_ID = "e2e_timeout_after_success"
OUT = ROOT / "traces" / f"{RUN_ID}.jsonl"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn, clock = db.connect(), FakeClock()
        env = MockEnvironment(conn=conn, clock=clock, scenario=Scenario(seed=1).force("book", 1, "timeout_booked"))
        mem = MemoryStore(conn, clock, "u_demo")
        seed_memories(mem)
        tracer = Tracer(RUN_ID, tmp)
        agent = Agent(conn, env, ScriptedModel(), clock, memory=mem, tracer=tracer, sleep=lambda s: None)
        for msg in ["book the same electrician as last time, not before 10am", "yes", "yes"]:
            print(f"user : {msg}\nagent: {agent.handle_user_message(msg)}\n")
        rows = db.rows(conn, "SELECT booking_id, provider_id, slot, status FROM mock_bookings WHERE idempotency_key IS NOT NULL")
        assert env.call_count("book") == 1 and len(rows) == 1, (env.call_count("book"), rows)
        OUT.parent.mkdir(exist_ok=True)
        shutil.copyfile(tracer.path, OUT)
        print(f"book calls={env.call_count('book')} status checks={env.call_count('get_booking_status')} bookings={rows}")
        print(f"wrote {OUT.relative_to(ROOT)} ({len(tracer._events)} events)")


if __name__ == "__main__":
    main()
