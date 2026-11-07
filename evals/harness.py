"""Eval harness: real Agent loop + ScriptedModel + forced mock Scenario + FakeClock.

Scenarios assert outcomes and invariants (booking counts, tool-call args, workflow state, the exact model
payloads from the context sidecar, trace events) and never reply wording.
"""
from __future__ import annotations

import json
from pathlib import Path

from errand_agent import db
from errand_agent.agent import Agent, Limits
from errand_agent.clock import FakeClock
from errand_agent.memory import MemoryStore, seed_memories, seed_prior_conversation
from errand_agent.mocks import MockEnvironment, Scenario
from errand_agent.models import ModelReply, ScriptedModel, ToolCall
from errand_agent.trace import Tracer

MON = "2026-11-09"   # FakeClock starts Sat 2026-11-07 09:00; Sundays are closed in the mock
SAT = "2026-11-14"
RUN_ID = "eval"


class Eval:
    """Per-scenario recorder; the conftest hook turns it into a row of EVAL_TABLE.md."""

    def __init__(self):
        self.actual_text = ""
        self.notes_text = ""

    def actual(self, text: str) -> None:
        self.actual_text = text

    def notes(self, text: str) -> None:
        self.notes_text = text


class World:
    def __init__(self, tmp_path: Path, scenario: Scenario | None = None, script=None, memory: bool = True,
                 limits: Limits | None = None, clock: FakeClock | None = None, persist: bool = False, timer=None):
        self.tmp = Path(tmp_path)
        self.runs = self.tmp / "runs"
        self.dbpath = self.tmp / "world.db"
        self.clock = clock or FakeClock()
        self.conn = db.connect(self.dbpath if persist else ":memory:")
        self.scenario = scenario or Scenario(seed=1)
        self.memory_on = memory
        self.limits, self.timer = limits, timer
        self.script = script
        self._boot(self.scenario, script)

    def _boot(self, scenario: Scenario, script) -> None:
        self.scenario = scenario
        self.env = MockEnvironment(conn=self.conn, clock=self.clock, scenario=scenario)
        self.mem = None
        if self.memory_on:
            self.mem = MemoryStore(self.conn, self.clock, "u_demo")
            if not self.mem.all(include_forgotten=True):
                seed_memories(self.mem)
        self.tracer = Tracer(RUN_ID, self.runs)
        self.model = ScriptedModel(script)
        kw = {"timer": self.timer} if self.timer else {}
        self.agent = Agent(self.conn, self.env, self.model, self.clock, memory=self.mem, tracer=self.tracer,
                           limits=self.limits, sleep=lambda s: None, **kw)

    def restart(self, scenario: Scenario | None = None, script=None) -> None:
        """Simulate a process restart: new Agent/env/tracer objects over the same SQLite file."""
        assert self.dbpath.exists(), "restart needs persist=True"
        self.conn.close()
        self.conn = db.connect(self.dbpath)
        self._boot(scenario or Scenario(seed=1), script)

    # ------------------------------------------------------------ driving
    def say(self, text: str) -> str:
        return self.agent.handle_user_message(text)

    def seed_history(self, n_copies: int = 3) -> None:
        for t in seed_prior_conversation() * n_copies:
            self.agent.store.add_turn(t["role"], t["content"])

    # ------------------------------------------------------------ observation
    def calls(self, tool: str) -> int:
        return self.env.call_count(tool)

    def new_bookings(self) -> list[dict]:
        """Bookings created by the agent (seed bookings have no idempotency key)."""
        return db.rows(self.conn, "SELECT * FROM mock_bookings WHERE idempotency_key IS NOT NULL")

    def booking(self, bid: str) -> dict:
        return db.one(self.conn, "SELECT * FROM mock_bookings WHERE booking_id=?", (bid,))

    def events(self, type_: str) -> list[dict]:
        return [e for e in self.tracer._events if e["type"] == type_]

    def tool_args(self, name: str) -> list[dict]:
        return [e["args"] for e in self.events("tool_call") if e["name"] == name]

    def pending(self):
        return self.agent.gate.active()

    def state(self) -> str:
        return self.agent.store.load().state

    def slots(self) -> dict:
        return self.agent.store.load().slots.to_dict()

    def exec_rows(self) -> list[dict]:
        return db.rows(self.conn, "SELECT * FROM wf_exec")

    def payloads(self) -> list[dict]:
        """Every context actually sent to the model (sidecar written before each model call)."""
        p = self.runs / f"{RUN_ID}.context.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]

    def payload_text(self) -> str:
        return "\n".join(r["system"] + "\n" + r["rendered"] for r in self.payloads())

    def snapshot(self) -> tuple:
        """Everything a consequential action could change."""
        return (self.calls("book"), self.calls("cancel"), self.calls("reschedule"),
                tuple(sorted((r["booking_id"], r["status"], r["slot"]) for r in db.rows(self.conn, "SELECT * FROM mock_bookings"))))


def call(name: str, **args) -> ToolCall:
    return ToolCall(name, args)


def reply(*calls: ToolCall, text: str = "") -> ModelReply:
    return ModelReply(text=text, tool_calls=list(calls))


def success_scenario(n: int = 1) -> Scenario:
    sc = Scenario(seed=1)
    for i in range(1, n + 1):
        sc.force("book", i, "success")
    return sc
