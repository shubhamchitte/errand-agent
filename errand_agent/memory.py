"""Persistent user memory (SQLite).

- source "explicit": user said it; always confirmed.
- source "inferred": agent guessed it; NOT usable until confirm() is called.
- forget() is a soft delete (kept for audit/inspect with include_forgotten) and
  forgotten facts are never returned by relevant().
- relevance is a deterministic tag/key score; no embeddings, no randomness.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from .clock import Clock
from .db import init_schema

DDL = """
CREATE TABLE IF NOT EXISTS memories (
  rowid_ INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('explicit','inferred')),
  confirmed INTEGER NOT NULL,
  forgotten INTEGER NOT NULL DEFAULT 0,
  category TEXT NOT NULL DEFAULT 'general',
  tags TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL,
  UNIQUE (user_id, key)
);
"""


@dataclass
class Fact:
    id: str
    key: str
    value: str
    source: Literal["explicit", "inferred"]
    confirmed: bool
    forgotten: bool
    category: str
    tags: list[str] = field(default_factory=list)
    updated_at: str = ""


def _fact(r: sqlite3.Row) -> Fact:
    return Fact(
        id=f"m{r['rowid_']:03d}", key=r["key"], value=r["value"], source=r["source"],
        confirmed=bool(r["confirmed"]), forgotten=bool(r["forgotten"]),
        category=r["category"], tags=json.loads(r["tags"]), updated_at=r["updated_at"],
    )


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, user_id: str):
        self.conn, self.clock, self.user_id = conn, clock, user_id
        init_schema(conn, DDL)

    def _get(self, key_or_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM memories WHERE user_id=? AND (key=? OR 'm'||substr('000'||rowid_,-3)=? OR 'm'||rowid_=?)",
            (self.user_id, key_or_id, key_or_id, key_or_id),
        ).fetchone()

    def remember(self, key: str, value: str, source: str = "explicit",
                 category: str = "general", tags=()) -> Fact:
        """Upsert by key. A new value supersedes the old one and revives a forgotten key.
        Inferred facts start unconfirmed; an inferred write never downgrades a confirmed
        explicit fact (explicit wins)."""
        if source not in ("explicit", "inferred"):
            raise ValueError(f"bad source {source!r}")
        now = self.clock.iso()
        cur = self._get(key)
        confirmed = 1 if source == "explicit" else 0
        tags_j = json.dumps(sorted(set(tags)))
        if cur is None:
            self.conn.execute(
                "INSERT INTO memories(user_id,key,value,source,confirmed,forgotten,category,tags,updated_at)"
                " VALUES(?,?,?,?,?,0,?,?,?)",
                (self.user_id, key, value, source, confirmed, category, tags_j, now))
        elif source == "inferred" and cur["source"] == "explicit" and not cur["forgotten"]:
            return _fact(cur)
        else:
            self.conn.execute(
                "UPDATE memories SET value=?,source=?,confirmed=?,forgotten=0,category=?,tags=?,updated_at=?"
                " WHERE rowid_=?", (value, source, confirmed, category, tags_j, now, cur["rowid_"]))
        self.conn.commit()
        return _fact(self._get(key))

    def confirm(self, fact_id: str) -> Fact:
        r = self._get(fact_id)
        if r is None:
            raise KeyError(fact_id)
        self.conn.execute("UPDATE memories SET confirmed=1,updated_at=? WHERE rowid_=?",
                          (self.clock.iso(), r["rowid_"]))
        self.conn.commit()
        return _fact(self._get(fact_id))

    def forget(self, key_or_id: str) -> bool:
        r = self._get(key_or_id)
        if r is None or r["forgotten"]:
            return False
        self.conn.execute("UPDATE memories SET forgotten=1,updated_at=? WHERE rowid_=?",
                          (self.clock.iso(), r["rowid_"]))
        self.conn.commit()
        return True

    def correct(self, key: str, new_value: str) -> Fact:
        """User correction: new value, becomes explicit + confirmed, un-forgotten."""
        r = self._get(key)
        if r is None:
            raise KeyError(key)
        self.conn.execute(
            "UPDATE memories SET value=?,source='explicit',confirmed=1,forgotten=0,updated_at=? WHERE rowid_=?",
            (new_value, self.clock.iso(), r["rowid_"]))
        self.conn.commit()
        return _fact(self._get(key))

    def get(self, key_or_id: str) -> Fact | None:
        r = self._get(key_or_id)
        return _fact(r) if r else None

    def all(self, include_forgotten: bool = False) -> list[Fact]:
        sql = "SELECT * FROM memories WHERE user_id=?" + ("" if include_forgotten else " AND forgotten=0")
        return [_fact(r) for r in self.conn.execute(sql + " ORDER BY rowid_", (self.user_id,))]

    def guesses_to_confirm(self, topics: set[str], exclude=()) -> list[Fact]:
        """Unconfirmed inferred facts whose tags touch one of ``topics`` (e.g. the service, 'time', 'price').
        These are never used or sent to the model; the agent may ask the user about them."""
        return [f for f in self.all() if f.source == "inferred" and not f.confirmed
                and f.id not in exclude and topics & set(f.tags)]

    def relevant(self, intent: str, service: str | None, slot_keys: list[str], limit: int) -> list[Fact]:
        """Deterministic: score = 3 per intent tag match + 3 service tag match
        + 2 per slot key matching a tag or fact key + 1 for 'global' tag (only if anything
        else matched or intent is a transactional one). Ties break by key. Score 0 -> excluded."""
        slots = set(slot_keys or [])
        scored: list[tuple[int, str, Fact]] = []
        for f in self.all():
            if f.source == "inferred" and not f.confirmed:
                continue
            tags = set(f.tags)
            s = 0
            if intent in tags:
                s += 3
            if service and service in tags:
                s += 3
            s += 2 * len(slots & (tags | {f.key}))
            if s and "global" in tags:
                s += 1
            if s == 0 and "global" in tags and intent in {"new_booking", "reschedule", "rebook_same_provider"}:
                s = 1
            if s:
                scored.append((-s, f.key, f))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [t[2] for t in scored[:max(0, limit)]]


# ---------------------------------------------------------------- seed data
# (key, value, source, confirmed, category, tags)
_RELEVANT = [
    ("pref.earliest_start", "Not before 10am on any day; never schedule visits earlier than 10:00", "explicit", True, "constraint",
     ["global", "new_booking", "reschedule", "rebook_same_provider", "time", "slot_time", "date", "window"]),
    ("pref.electrician", "Preferred electrician is p_elc_1 (Ravi); rebook him when available", "explicit", True, "provider",
     ["electrician", "provider", "rebook_same_provider", "new_booking", "provider_id"]),
    ("pref.plumber", "Happy with p_plm_1 for plumbing jobs", "explicit", True, "provider",
     ["plumber", "provider", "rebook_same_provider", "provider_id"]),
    ("pref.ac_tech", "AC technician p_ac_2 serviced the split AC well last summer", "explicit", True, "provider",
     ["ac_repair", "provider", "rebook_same_provider", "reschedule", "provider_id"]),
    ("budget.typical_visit", "Usually spends up to Rs 1500 for a minor home visit", "explicit", True, "budget",
     ["global", "new_booking", "budget", "price", "max_price"]),
    ("budget.hard_cap", "Ask before anything above Rs 3000", "explicit", True, "budget",
     ["global", "new_booking", "budget", "price", "max_price", "approval"]),
    ("budget.dislikes_surge", "Dislikes surge or emergency premium pricing", "explicit", True, "budget",
     ["new_booking", "price", "budget"]),
    ("area.locality", "Lives in Indiranagar, Bengaluru; pin 560038", "explicit", True, "location",
     ["global", "new_booking", "address", "locality", "location"]),
    ("area.address_notes", "Flat 4B, second floor, no lift; landmark: opposite the pharmacy", "explicit", True, "location",
     ["new_booking", "address", "location"]),
    ("access.gate_style", "Gate code is shared by the user only at booking time; never store the code itself", "explicit", True, "access",
     ["new_booking", "address", "access", "notes"]),
    ("access.call_first", "Technician should call 15 minutes before arriving", "explicit", True, "access",
     ["new_booking", "reschedule", "notes"]),
    ("household.pets", "Has a dog (Bruno); mention in notes so the technician is aware", "explicit", True, "household",
     ["new_booking", "notes", "pets", "rebook_same_provider"]),
    ("household.weekday_wfh", "Works from home Tue and Thu; those days are fine for visits after 10am", "explicit", True, "schedule",
     ["new_booking", "reschedule", "date", "time", "slot_time"]),
    ("schedule.weekend_pref", "Prefers weekday visits; weekends only for emergencies", "explicit", True, "schedule",
     ["new_booking", "reschedule", "date"]),
    ("schedule.no_lunch", "Avoid 1pm to 2pm, family lunch", "explicit", True, "schedule",
     ["new_booking", "reschedule", "time", "slot_time"]),
    ("service.ac_model", "Split AC, 1.5 ton, serviced twice a year", "explicit", True, "appliance",
     ["ac_repair", "reschedule", "new_booking", "notes"]),
    ("service.plumbing_issue", "Kitchen sink tap has a slow drip, recurring", "explicit", True, "appliance",
     ["plumber", "new_booking", "notes"]),
    ("service.cleaning_pref", "Eco-friendly cleaning products only", "explicit", True, "preference",
     ["cleaning", "new_booking", "notes"]),
    ("service.painting_color", "Wall colour preference: warm off-white", "explicit", True, "preference",
     ["painter", "new_booking", "notes"]),
    ("service.pest_control", "Pet-safe pest control only", "explicit", True, "preference",
     ["pest_control", "new_booking", "pets", "notes"]),
    ("comm.language", "Prefers English with Hindi acceptable for technician calls", "explicit", True, "comms",
     ["global", "notes"]),
    ("comm.confirmation", "Always confirm the final booking details before booking", "explicit", True, "comms",
     ["global", "new_booking", "reschedule", "cancel", "approval"]),
    ("cancel.policy_note", "Dislikes cancellation fees; prefers reschedule over cancel", "explicit", True, "preference",
     ["cancel", "reschedule", "price"]),
    ("inferred.morning_person", "Probably prefers late morning slots (11am-12pm)", "inferred", True, "schedule",
     ["new_booking", "reschedule", "time", "slot_time"]),
    ("inferred.rating_threshold", "Probably wants providers rated 4.5 or above", "inferred", True, "preference",
     ["new_booking", "provider"]),
    ("inferred.upi", "Probably pays by UPI", "inferred", False, "payment", ["new_booking", "payment"]),
    ("inferred.ac_tech_loyal", "Probably wants the same AC tech again", "inferred", False, "provider",
     ["ac_repair", "rebook_same_provider", "reschedule"]),
    ("inferred.cheapest", "Probably wants the cheapest option", "inferred", False, "budget",
     ["new_booking", "price"]),
    ("inferred.evening", "Probably free after 6pm", "inferred", False, "schedule",
     ["new_booking", "reschedule", "time"]),
]
_FORGOTTEN = [
    ("old.electrician", "Preferred electrician was p_elc_9", "explicit", True, "provider",
     ["electrician", "provider", "rebook_same_provider"]),
    ("old.address", "Lived in Koramangala", "explicit", True, "location", ["global", "new_booking", "address"]),
    ("old.earliest_start", "Anytime after 8am is fine", "explicit", True, "constraint",
     ["global", "new_booking", "reschedule", "time"]),
]
_IRRELEVANT = [
    ("diet.type", "Vegetarian"), ("diet.allergy", "Allergic to peanuts"), ("diet.coffee", "Two coffees a day, no sugar"),
    ("diet.breakfast", "Usually has poha for breakfast"), ("diet.spice", "Likes medium spice"),
    ("hobby.guitar", "Plays acoustic guitar on weekends"), ("hobby.running", "Runs 5k on Sunday mornings"),
    ("hobby.chess", "Online chess, rated about 1400"), ("hobby.books", "Reads science fiction"),
    ("hobby.photography", "Amateur bird photography"), ("music.genre", "Likes jazz and indie rock"),
    ("travel.next", "Planning a trip to Goa in March"), ("travel.seat", "Prefers aisle seats"),
    ("travel.airline", "Frequent flyer with a budget airline"), ("work.title", "Product manager at a fintech"),
    ("work.tools", "Uses Notion and Slack daily"), ("work.meeting_day", "Team standup at 9:30 on weekdays"),
    ("family.sibling", "Has one younger sister"), ("family.birthday", "Mother's birthday in April"),
    ("health.gym", "Gym membership at a nearby studio"), ("health.sleep", "Sleeps around 11pm"),
    ("media.tv", "Watching a crime documentary series"), ("media.podcast", "Listens to tech podcasts"),
    ("shop.brand", "Prefers one particular sneaker brand"), ("shop.grocery", "Orders groceries on Saturdays"),
    ("car.model", "Drives a hatchback"), ("car.service", "Car service due in December"),
    ("tech.phone", "Uses an Android phone"), ("tech.laptop", "Laptop is a 14 inch ultrabook"),
    ("fav.color", "Favourite colour is green"), ("fav.food", "Loves dosa"), ("fav.movie", "Likes sci-fi movies"),
]


def seed_memories(store: MemoryStore) -> list[Fact]:
    """Seed >=50 facts for the demo user: relevant, irrelevant, inferred (confirmed/unconfirmed), forgotten."""
    out: list[Fact] = []
    for key, val, src, conf, cat, tags in _RELEVANT + _FORGOTTEN:
        f = store.remember(key, val, source=src, category=cat, tags=tags)
        if conf and src == "inferred":
            f = store.confirm(key)
        out.append(f)
    for key, val in _IRRELEVANT:
        out.append(store.remember(key, val, category="personal", tags=["personal", key.split(".")[0]]))
    for key, *_ in _FORGOTTEN:
        store.forget(key)
    return [store.get(f.key) for f in out]


_TOPICS = [
    ("Any dinner ideas for tonight?", "Since you like medium spice and dosa, how about a veg dosa night?"),
    ("My guitar strings snapped again.", "Annoying! Fresh strings are cheap; want a reminder to restring?"),
    ("The AC at home is making a rattling noise.", "That could be a loose fan blade. I can look for AC technicians when you want."),
    ("Not now, I am in a meeting.", "No problem, I will be here."),
    ("Any good jazz albums?", "Try Kind of Blue or something more recent like Kamasi Washington."),
    ("The kitchen tap is dripping again.", "I can find a plumber. p_plm_1 worked well before."),
    ("Remind me my sister's birthday gift budget.", "I do not have a gift budget stored, only your home service budget."),
    ("Bruno was barking at the technician last time.", "Noted, I will mention the dog in booking notes."),
    ("What is a good 5k pace?", "For a recreational runner around 6:00 to 6:30 per km is solid."),
    ("I do not like paying surge prices.", "Understood, I will flag any premium pricing before booking."),
    ("Is Goa nice in March?", "Warm and pleasant, before the monsoon rush."),
    ("Please do not book anything before 10am.", "Got it: nothing before 10:00."),
]


def seed_prior_conversation() -> list[dict]:
    """Long (>=60 turns) deterministic prior conversation, mostly irrelevant chatter plus a few home-service remarks."""
    turns: list[dict] = []
    i = 0
    while len(turns) < 72:
        q, a = _TOPICS[i % len(_TOPICS)]
        suffix = "" if i < len(_TOPICS) else f" (follow-up {i // len(_TOPICS)})"
        turns.append({"role": "user", "content": q + suffix})
        turns.append({"role": "assistant", "content": a})
        i += 1
    return turns
