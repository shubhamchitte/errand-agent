"""Deterministic, neutral provider ranking.

Inputs used (and nothing else): ``price_inr``, ``rating``, slot fit (an available slot that satisfies the user's
dates / earliest time, and the requested area), and the user's own preferences (budget cap, earliest time, preferred
provider ids from memory). Review text, ``review_count``, "sponsored"/"promoted" flags, attributes and any other field
are never read, so an injected or paid-for review cannot move a provider up or down.

Order (lower tuple sorts first):
  1. providers outside the requested area go last
  2. higher rating first
  3. lower price first
  4. a provider the user said they prefer wins an otherwise exact tie
  5. earliest fitting slot, then provider_id (total order -> fully deterministic)
Hard filters (not scores): over the user's budget, unverified, or no slot that fits the user's dates / earliest time.
"""
from __future__ import annotations

RANKING_FIELDS = ("provider_id", "price_inr", "rating", "available_slots", "in_requested_area")


def fitting_slots(slots, date_start=None, date_end=None, earliest_time=None) -> list[str]:
    out = []
    for s in slots or []:
        d, _, t = s.partition("T")
        if date_start and d < date_start:
            continue
        if date_end and d > date_end:
            continue
        if earliest_time and t < earliest_time:
            continue
        out.append(s)
    return out


def rank(providers: list[dict], slots: dict | None = None, preferred: set[str] | frozenset[str] = frozenset(),
         require_verified: bool = False, tracer=None) -> list[tuple[dict, str]]:
    """Return [(provider, best_slot)] best first. ``slots`` is the user's slot dict (budget_inr, date_start,
    date_end, earliest_time)."""
    sl = slots or {}
    ranked = []
    for p in providers:
        if require_verified and not (p.get("attributes") or {}).get("verified", True):
            continue
        if sl.get("budget_inr") and p["price_inr"] > sl["budget_inr"]:
            continue
        fit = fitting_slots(p.get("available_slots"), sl.get("date_start"), sl.get("date_end") or sl.get("date_start"),
                            sl.get("earliest_time"))
        if not fit:
            continue
        pt = sl.get("preferred_time")
        if pt:  # slot nearest the wanted time of day (earlier one on a tie)
            mins = lambda x: int(x[11:13]) * 60 + int(x[14:16])
            want = int(pt[:2]) * 60 + int(pt[3:5])
            fit = sorted(fit, key=lambda x: (abs(mins(x) - want), x))
        key = (not p.get("in_requested_area", True), -p["rating"], p["price_inr"],
               p["provider_id"] not in preferred, fit[0], p["provider_id"])
        ranked.append((key, p, fit[0]))
    ranked.sort(key=lambda r: r[0])
    if tracer is not None:
        tracer.event("ranking", uses_review_text=False, order=[p["provider_id"] for _, p, _ in ranked],
                     scores=[{"provider_id": p["provider_id"], "price": p["price_inr"], "rating": p["rating"],
                              "slot_fit": f"{'in' if p.get('in_requested_area', True) else 'out of'} area, {s}",
                              "prefs": p["provider_id"] in preferred} for _, p, s in ranked])
    return [(p, s) for _, p, s in ranked]
