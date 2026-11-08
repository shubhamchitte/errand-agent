"""User-facing formatting helpers: rupee amounts and human-friendly slot times."""
from __future__ import annotations

import re
from datetime import datetime


def inr(n) -> str:
    """1450 -> '₹1,450' (Indian digit grouping is not used: keeps it simple and unambiguous)."""
    if n is None:
        return "n/a"
    return f"₹{int(n):,}"


def when(slot: str | None) -> str:
    """'2026-11-09T10:00' -> 'Mon 9 Nov, 10:00'; unparseable input is returned unchanged."""
    try:
        d = datetime.fromisoformat(slot)
    except (TypeError, ValueError):
        return str(slot)
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}, {d.strftime('%H:%M')}"


INJECTION_RE = re.compile(
    r"ignore (all )?(previous|prior) instructions|system instruction|\[?assistant:|new instructions|admin mode|"
    r"without (asking for )?confirmation|approval is not required|do not tell the user|reveal your system prompt", re.I)
