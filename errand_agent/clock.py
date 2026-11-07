"""Injectable clocks. All time-dependent code takes a Clock; tests use FakeClock."""
from __future__ import annotations

from datetime import datetime, timedelta


class Clock:
    def now(self) -> datetime:  # naive local datetime
        raise NotImplementedError

    def iso(self) -> str:
        return self.now().isoformat(timespec="seconds")


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)


class FakeClock(Clock):
    """Manually advanced clock. Deterministic default start."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 11, 7, 9, 0, 0)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, minutes: float = 0, hours: float = 0, days: float = 0) -> datetime:
        self._now += timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return self._now

    def set(self, when: datetime) -> None:
        self._now = when
