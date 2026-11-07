"""Persisted workflow state machine + structured slots (SQLite).

One workflow row per user. Slots are structured facts; a correction updates only the
fields the user mentioned (see ``Slots.merge``).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field, fields

from . import db
from .clock import Clock

# ---------------------------------------------------------------- states
IDLE = "IDLE"
GATHERING = "GATHERING"
SEARCHING = "SEARCHING"
COMPARING = "COMPARING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
EXECUTING = "EXECUTING"
VERIFYING = "VERIFYING"
DONE = "DONE"
FAILED = "FAILED"
ESCALATED = "ESCALATED"

STATES = {IDLE, GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, EXECUTING, VERIFYING, DONE, FAILED, ESCALATED}
_ENTRY = {IDLE, GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, DONE, FAILED, ESCALATED}
# Allowed transitions. EXECUTING/VERIFYING are only reachable through the approval gate.
TRANSITIONS: dict[str, set[str]] = {
    IDLE: {GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, ESCALATED},
    GATHERING: {GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, IDLE, ESCALATED, FAILED},
    SEARCHING: {GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, IDLE, FAILED, ESCALATED},
    COMPARING: {GATHERING, SEARCHING, COMPARING, AWAITING_APPROVAL, IDLE, FAILED, ESCALATED},
    AWAITING_APPROVAL: {EXECUTING, AWAITING_APPROVAL, GATHERING, SEARCHING, COMPARING, IDLE, ESCALATED},
    EXECUTING: {VERIFYING, DONE, FAILED, AWAITING_APPROVAL, COMPARING, ESCALATED},
    VERIFYING: {DONE, FAILED, EXECUTING, ESCALATED, AWAITING_APPROVAL},
    DONE: _ENTRY,
    FAILED: _ENTRY,
    ESCALATED: _ENTRY,
}

INTENTS = ("new_booking", "reschedule", "cancel", "rebook_same_provider", "memory_command", "smalltalk")
REQUIRED_SLOTS = {
    "new_booking": ["service", "area", "date_start"],
    "rebook_same_provider": ["service"],
    "reschedule": [],
    "cancel": [],
}


class IllegalTransition(Exception):
    pass


@dataclass
class Slots:
    service: str | None = None
    area: str | None = None
    date_start: str | None = None   # YYYY-MM-DD
    date_end: str | None = None
    earliest_time: str | None = None  # HH:MM
    budget_inr: int | None = None
    provider_id: str | None = None
    booking_id: str | None = None
    slot: str | None = None  # requested ISO slot (reschedule / explicit)

    def merge(self, update: dict) -> list[str]:
        """Apply only non-None fields from ``update``. Returns names of fields that changed."""
        changed = []
        for f in fields(self):
            v = update.get(f.name)
            if v is not None and getattr(self, f.name) != v:
                setattr(self, f.name, v)
                changed.append(f.name)
        if "date_start" in changed and update.get("date_end") is None and self.date_end and self.date_end < self.date_start:
            self.date_end = self.date_start
        return changed

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> "Slots":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


@dataclass
class Workflow:
    user_id: str
    state: str = IDLE
    intent: str | None = None
    slots: Slots = field(default_factory=Slots)
    notes: dict = field(default_factory=dict)  # small structured scratch (last search ids, alternatives...)
    updated_at: str = ""

    def missing(self) -> list[str]:
        req = REQUIRED_SLOTS.get(self.intent or "", [])
        return [k for k in req if getattr(self.slots, k) is None]

    def summary(self) -> str:
        s = self.slots.to_dict()
        parts = [f"state={self.state}", f"intent={self.intent or '-'}",
                 "slots=" + (json.dumps(s, sort_keys=True) if s else "{}")]
        miss = self.missing()
        if miss:
            parts.append("missing=" + ",".join(miss))
        return "; ".join(parts)


DDL = """
CREATE TABLE IF NOT EXISTS wf_workflow(
  user_id TEXT PRIMARY KEY, state TEXT, intent TEXT, slots TEXT, notes TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS wf_turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, role TEXT, content TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS wf_pending(
  action_id TEXT PRIMARY KEY, user_id TEXT, type TEXT, card TEXT, hash TEXT, status TEXT,
  hold_id TEXT, hold_expires_at TEXT, created_at TEXT, updated_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS wf_exec(
  idempotency_key TEXT PRIMARY KEY, action_id TEXT, user_id TEXT, type TEXT, args TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, result TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS wf_user_cancelled(booking_id TEXT PRIMARY KEY, ts TEXT);
"""


class WorkflowStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, user_id: str, tracer=None):
        self.conn, self.clock, self.user_id, self.tracer = conn, clock, user_id, tracer
        db.init_schema(conn, DDL)

    # ----------------------------------------------------------- workflow
    def load(self) -> Workflow:
        r = db.one(self.conn, "SELECT * FROM wf_workflow WHERE user_id=?", (self.user_id,))
        if not r:
            return Workflow(self.user_id)
        return Workflow(self.user_id, r["state"], r["intent"], Slots.from_dict(json.loads(r["slots"] or "{}")),
                        json.loads(r["notes"] or "{}"), r["updated_at"])

    def save(self, wf: Workflow) -> None:
        wf.updated_at = self.clock.iso()
        self.conn.execute("INSERT OR REPLACE INTO wf_workflow VALUES(?,?,?,?,?,?)",
                          (wf.user_id, wf.state, wf.intent, json.dumps(wf.slots.to_dict()), json.dumps(wf.notes),
                           wf.updated_at))
        self.conn.commit()

    def transition(self, wf: Workflow, to: str, reason: str = "") -> None:
        if to not in STATES:
            raise IllegalTransition(to)
        if to != wf.state and to not in TRANSITIONS[wf.state]:
            raise IllegalTransition(f"{wf.state} -> {to} ({reason})")
        frm, wf.state = wf.state, to
        self.save(wf)
        if self.tracer is not None and frm != to:
            self.tracer.state_transition(frm, to, reason)

    # -------------------------------------------------------------- turns
    def add_turn(self, role: str, content: str) -> None:
        self.conn.execute("INSERT INTO wf_turns(user_id,role,content,ts) VALUES(?,?,?,?)",
                          (self.user_id, role, content, self.clock.iso()))
        self.conn.commit()

    def turns(self, limit: int = 50) -> list[dict]:
        rs = db.rows(self.conn, "SELECT role, content FROM wf_turns WHERE user_id=? ORDER BY id DESC LIMIT ?",
                     (self.user_id, limit))
        return list(reversed(rs))

    def mark_user_cancelled(self, booking_id: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO wf_user_cancelled VALUES(?,?)", (booking_id, self.clock.iso()))
        self.conn.commit()

    def user_cancelled(self, booking_id: str) -> bool:
        return db.one(self.conn, "SELECT 1 x FROM wf_user_cancelled WHERE booking_id=?", (booking_id,)) is not None
