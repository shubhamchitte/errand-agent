"""Deterministic intent router + slot extractor (no model call).

Returns the intent and ONLY the slot fields explicitly present in the message, so that a
correction ("actually Koramangala") changes one field and nothing else.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .mocks import data as mock_data

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Hinglish (romanised Hindi) support is a small explicit vocabulary, not a translator: yes/no words, relative days,
# weekday names, "<n> tak" budgets, "<n> baje ke baad" times, and a few verbs (chahiye, shift/badlo, raddh).
_HI_WEEKDAYS = {"somvar": 0, "mangalvar": 1, "budhvar": 2, "guruvar": 3, "veervar": 3, "shukravar": 4,
                "shanivar": 5, "shaniwar": 5, "ravivar": 6, "itwar": 6}

_AFFIRM = re.compile(r"^\s*(yes|haan kar do|haan karo|haan ji|haan|han|haa|ha|theek hai|thik hai|kar do|karo|yep|yeah|y|ok|okay|sure|confirm(ed)?|approve(d)?|go ahead|do it|book it|please do|"
                     r"proceed|sounds good|yes please|go for it)\b[\s!.,]*(please|it|that|now)?[\s!.]*$", re.I)
_NEGATE = re.compile(r"\b(no|nahi|nahin|mat karo|ruko|nope|don'?t|do not|stop|cancel that|wait|hold on|not yet|never ?mind|decline)\b", re.I)


def is_affirmative(text: str) -> bool:
    """Strict: a short, unambiguous yes. Anything containing a negation or extra request is not approval."""
    t = text.strip()
    if not t or len(t) > 40 or _NEGATE.search(t):
        return False
    return bool(_AFFIRM.match(t))


def is_negative(text: str) -> bool:
    t = text.strip().lower()
    return bool(re.match(r"^(no|nahi|nahin|mat karo|ruko|nope|don'?t|do not|stop|not now|never ?mind|decline|cancel that|forget it)\b", t))


@dataclass
class Routed:
    intent: str
    slots: dict = field(default_factory=dict)
    memory_op: str | None = None  # show | forget | remember | correct
    memory_arg: str | None = None
    is_correction: bool = False


def _service(t: str) -> str | None:
    best = None
    for alias, svc in sorted(mock_data.SERVICE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            best = svc
            break
    return best


def _area(t: str) -> str | None:
    for a in mock_data.AREAS:
        if a.lower() in t:
            return a
    return None


def _time(t: str) -> str | None:
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*baje\s*(?:ke\s*)?(?:baad|k baad)", t)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        h = h + 12 if ap == "pm" and h < 12 else (0 if ap == "am" and h == 12 else h)
        return f"{h:02d}:{mi:02d}" if 0 <= h < 24 else None
    m = re.search(r"(?:not before|after|from|no earlier than|earliest|later than)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if 0 <= h < 24 else None


def _budget(t: str) -> int | None:
    m = re.search(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*)\s*(?:rs|rupay[ae]?|rupees)?\s*(?:tak|ke andar|se kam|max)\b", t)
    if m:
        return int(m.group(1).replace(",", ""))
    m = re.search(r"(?:under|below|budget(?: of| is)?|max(?:imum)?|upto|up to|within|less than)\s*(?:₹|rs\.?|inr)?\s*(\d[\d,]*)", t)
    if not m:
        m = re.search(r"(?:₹|rs\.?\s?|inr\s?)(\d[\d,]*)", t)
    return int(m.group(1).replace(",", "")) if m else None


def _dates(t: str, today: date) -> tuple[str | None, str | None]:
    m = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if m:
        return m[0], m[-1]
    if re.search(r"\bparso\b", t):
        d = today + timedelta(days=2)
        return d.isoformat(), d.isoformat()
    if re.search(r"\bkal\b", t):  # "kal" means both yesterday and tomorrow; for bookings it is always tomorrow
        d = today + timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if re.search(r"\baaj\b", t):
        return today.isoformat(), today.isoformat()
    if re.search(r"\bagle hafte\b", t):
        mon = today + timedelta(days=7 - today.weekday())
        return mon.isoformat(), (mon + timedelta(days=6)).isoformat()
    for wd, i in _HI_WEEKDAYS.items():
        if re.search(rf"\b{wd}\b", t):
            d = today + timedelta(days=(i - today.weekday()) % 7 or 7)
            return d.isoformat(), d.isoformat()
    if "day after tomorrow" in t:
        d = today + timedelta(days=2)
        return d.isoformat(), d.isoformat()
    if "tomorrow" in t:
        d = today + timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if re.search(r"\btoday\b", t):
        return today.isoformat(), today.isoformat()
    if "this weekend" in t or "weekend" in t:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        return sat.isoformat(), (sat + timedelta(days=1)).isoformat()
    if "next week" in t:
        mon = today + timedelta(days=7 - today.weekday())
        return mon.isoformat(), (mon + timedelta(days=6)).isoformat()
    if "this week" in t:
        return today.isoformat(), (today + timedelta(days=6 - today.weekday())).isoformat()
    for i, wd in enumerate(_WEEKDAYS):
        if re.search(rf"\b{wd}\b", t):
            d = today + timedelta(days=(i - today.weekday()) % 7 or 7)
            return d.isoformat(), d.isoformat()
    return None, None


def _slot(t: str) -> str | None:
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})[t ](\d{2}:\d{2})\b", t)
    return f"{m.group(1)}T{m.group(2)}" if m else None


def extract_slots(text: str, now: datetime) -> dict:
    t = text.lower()
    out: dict = {}
    if (s := _service(t)):
        out["service"] = s
    if (a := _area(t)):
        out["area"] = a
    ds, de = _dates(t, now.date())
    if ds:
        out["date_start"], out["date_end"] = ds, de
    if (tm := _time(t)):
        out["earliest_time"] = tm
    if (b := _budget(t)) is not None:
        out["budget_inr"] = b
    if (m := re.search(r"\b(p_[a-z]+_\d+)\b", t)):
        out["provider_id"] = m.group(1)
    if (m := re.search(r"\b(bk_[a-z0-9_]+)\b", t)):
        out["booking_id"] = m.group(1)
    if (sl := _slot(t)):
        out["slot"] = sl
    return out


def route(text: str, now: datetime, current_intent: str | None = None) -> Routed:
    t = text.lower().strip()
    slots = extract_slots(text, now)
    # memory commands
    if re.search(r"what do you (remember|know)|show (me )?(my )?memor|my preferences|list memor|"
                 r"mere baare mein|meri preferences|kya yaad hai", t):
        return Routed("memory_command", {}, "show")
    m = re.match(r"^\s*forget\s+(?:that\s+|about\s+|my\s+)*(.+?)[.!]?\s*$", t)
    if m:
        return Routed("memory_command", {}, "forget", m.group(1))
    m = re.match(r"^\s*remember(?: that)?\s+(.+?)[.!]?\s*$", t)
    if m:
        return Routed("memory_command", slots, "remember", m.group(1))
    correction = bool(re.match(r"^\s*(actually|sorry|correction|i meant|make it|change (it|that)|instead)\b", t))
    if re.search(r"\b(cancel|raddh|radd)\b", t) and not re.search(r"cancel that\b", t):
        return Routed("cancel", slots)
    if re.search(r"\b(reschedule|move|shift|postpone|push|badlo|badal do|aage karo)\b", t):
        return Routed("reschedule", slots, is_correction=correction)
    if re.search(r"\b(same|again|last time|usual|previous)\b", t) and (slots.get("service") or "same" in t):
        return Routed("rebook_same_provider", slots, is_correction=correction)
    if correction and current_intent:
        return Routed(current_intent, slots, is_correction=True)
    if slots.get("service") or re.search(r"\b(book|need|find|get me|looking for|search|chahiye|chahie|dhundo|bulao)\b", t):
        if current_intent in ("new_booking", "rebook_same_provider", "reschedule") and not slots.get("service") \
                and not re.search(r"\bbook\b", t):
            return Routed(current_intent, slots)
        return Routed("new_booking", slots)
    if slots and current_intent:
        # bare fact ("Koramangala", "tomorrow after 10am") continues the current flow
        return Routed(current_intent, slots, is_correction=correction)
    return Routed("smalltalk", slots)
