"""Scenario config: makes mock behaviour reproducible and forceable."""
from __future__ import annotations

from dataclasses import dataclass, field

# Outcomes that can be forced for the Nth call of a tool (1-based):
#   book:       success | slot_taken | timeout_booked | timeout_not_booked | hold_expired
#   reschedule: success | slot_conflict
#   any tool:   transient_error (raises TransientError 503, nothing executed)
#               timeout (raises ToolTimeout, nothing executed)
FORCEABLE = {"success", "slot_taken", "timeout_booked", "timeout_not_booked", "hold_expired",
             "slot_conflict", "transient_error", "timeout"}


@dataclass
class Scenario:
    seed: int = 42
    # {("book", 1): "timeout_booked"}  -> first call of book times out but actually books
    forced: dict[tuple[str, int], str] = field(default_factory=dict)
    # quoted price = search price + drift. Per provider overrides global.
    quote_drift: int = 0
    provider_quote_drift: dict[str, int] = field(default_factory=dict)
    # Demo aid, only until the first booking exists: a (provider, slot) is first held for
    # `first_booking_hold_minutes`, so the executor re-holds at approval time, and that re-hold quotes
    # `first_booking_rehold_drift` more. Both are derived from the DB, so they survive restarts.
    first_booking_hold_minutes: int | None = None
    first_booking_rehold_drift: int = 0
    # slots (ISO "YYYY-MM-DDTHH:MM") that always cause slot_conflict on reschedule
    conflict_slots: set[str] = field(default_factory=set)
    hold_minutes: int = 10
    cancel_transient_first: bool = True  # first cancel call per booking -> 503
    # book outcome probabilities (cumulative order): success, slot_taken, timeout(booked/not 50:50)
    p_success: float = 0.70
    p_slot_taken: float = 0.15

    def force(self, tool: str, n: int, outcome: str) -> "Scenario":
        assert outcome in FORCEABLE, outcome
        self.forced[(tool, n)] = outcome
        return self
