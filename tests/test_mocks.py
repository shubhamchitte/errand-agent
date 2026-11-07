import collections

import pytest

from errand_agent import db
from errand_agent.clock import FakeClock
from errand_agent.mocks import MockEnvironment, Scenario, TOOL_NAMES, TOOL_SCHEMAS, ToolTimeout, TransientError


def mk(scenario=None, conn=None, clock=None):
    return MockEnvironment(conn=conn, clock=clock or FakeClock(), scenario=scenario or Scenario())


def first_hold(env, pid="p_plm_1", idx=0):
    s = env.search_providers("plumber", "Indiranagar", None)
    p = next(x for x in s["providers"] if x["provider_id"] == pid)
    return env.hold_slot(pid, p["available_slots"][idx])


def test_search_contract():
    env = mk()
    for svc in ["plumber", "electrician", "ac_servicing", "AC servicing", "plumbing"]:
        r = env.search_providers(svc, "Indiranagar", None)
        assert 3 <= len(r["providers"]) <= 8
        for p in r["providers"]:
            assert {"provider_id", "price_inr", "rating", "available_slots", "attributes"} <= set(p)
    assert env.search_providers("gardener", "x", None)["status"] == "unknown_service"


def test_search_date_range_and_area_ranking():
    env = mk()
    r = env.search_providers("plumber", "Koramangala", {"start": "2026-11-09", "end": "2026-11-10"})
    assert all(s[:10] in ("2026-11-09", "2026-11-10") for p in r["providers"] for s in p["available_slots"])
    assert r["providers"][0]["area"] == "Koramangala"
    r2 = env.search_providers("plumber", "Koramangala", "2026-11-09/2026-11-09")
    assert all(s.startswith("2026-11-09") for p in r2["providers"] for s in p["available_slots"])


def test_get_provider_has_injection_review():
    env = mk()
    texts = [rv["text"].lower() for pid in ("p_plm_2", "p_elc_2", "p_ac_5") for rv in env.get_provider(pid)["reviews"]]
    assert any("ignore previous instructions" in t and "book" in t for t in texts)
    assert any("cancel" in t and "budget" in t for t in texts)
    assert env.get_provider("nope")["status"] == "not_found"


def test_hold_expiry_and_quote_drift():
    env = mk(Scenario(provider_quote_drift={"p_plm_1": 300}))
    h = first_hold(env)
    assert h["status"] == "held" and h["quoted_price"] == h["search_price"] + 300
    assert h["expires_at"] == "2026-11-07T09:10:00"
    assert env.hold_slot("p_plm_1", "2030-01-01T00:00")["status"] == "slot_unavailable"
    env2 = mk(Scenario(quote_drift=-100))
    assert first_hold(env2)["quoted_price"] == 1100


def test_book_hold_expired():
    clock = FakeClock()
    env = mk(clock=clock)
    h = first_hold(env)
    clock.advance(minutes=10, seconds=1)
    assert env.book(h["hold_id"], "k1")["status"] == "hold_expired"
    assert env.get_booking_status(hold_id=h["hold_id"])["hold_state"] == "expired"
    assert env.book(h["hold_id"], "k1")["status"] == "hold_expired"  # same key, same answer


def test_book_not_expired_at_boundary():
    clock = FakeClock()
    env = mk(Scenario().force("book", 1, "success"), clock=clock)
    h = first_hold(env)
    clock.advance(minutes=10)
    assert env.book(h["hold_id"], "k")["status"] == "confirmed"


def test_book_idempotent_same_key_single_booking():
    env = mk(Scenario().force("book", 1, "success"))
    h = first_hold(env)
    a = env.book(h["hold_id"], "k1")
    b = env.book(h["hold_id"], "k1")
    assert a == b and a["status"] == "confirmed"
    n = db.one(env.conn, "SELECT COUNT(*) c FROM mock_bookings WHERE hold_id=?", (h["hold_id"],))["c"]
    assert n == 1
    # even after expiry the same key still returns the booking
    env.clock.advance(minutes=30)
    assert env.book(h["hold_id"], "k1") == a


def test_book_different_key_on_booked_hold_is_slot_taken():
    env = mk(Scenario().force("book", 1, "success"))
    h = first_hold(env)
    env.book(h["hold_id"], "k1")
    assert env.book(h["hold_id"], "k2")["status"] == "slot_taken"
    assert db.one(env.conn, "SELECT COUNT(*) c FROM mock_bookings WHERE hold_id=?", (h["hold_id"],))["c"] == 1


def test_forced_slot_taken_removes_slot_from_search():
    env = mk(Scenario().force("book", 1, "slot_taken"))
    h = first_hold(env)
    assert env.book(h["hold_id"], "k")["status"] == "slot_taken"
    assert env.book(h["hold_id"], "k")["status"] == "slot_taken"
    assert env.hold_slot("p_plm_1", h["slot"])["status"] == "slot_unavailable"


def test_timeout_booked_visible_via_status_and_retry_no_duplicate():
    env = mk(Scenario().force("book", 1, "timeout_booked"))
    h = first_hold(env)
    with pytest.raises(ToolTimeout):
        env.book(h["hold_id"], "k")
    for st in (env.get_booking_status(hold_id=h["hold_id"]), env.get_booking_status(idempotency_key="k")):
        assert st["status"] == "booked" and st["booking_id"]
    r = env.book(h["hold_id"], "k")
    assert r["status"] == "confirmed" and r["booking_id"] == st["booking_id"]
    assert db.one(env.conn, "SELECT COUNT(*) c FROM mock_bookings WHERE hold_id=?", (h["hold_id"],))["c"] == 1


def test_timeout_not_booked_status_and_retry():
    env = mk(Scenario().force("book", 1, "timeout_not_booked").force("book", 2, "success"))
    h = first_hold(env)
    with pytest.raises(ToolTimeout):
        env.book(h["hold_id"], "k")
    st = env.get_booking_status(idempotency_key="k", hold_id=h["hold_id"])
    assert st["status"] == "not_booked" and st["hold_state"] == "active"
    assert env.book(h["hold_id"], "k")["status"] == "confirmed"


def test_book_probabilities_roughly_70_15_15():
    counts = collections.Counter()
    for seed in range(400):
        env = mk(Scenario(seed=seed))
        h = first_hold(env, idx=0)
        try:
            counts[env.book(h["hold_id"], "k")["status"]] += 1
        except ToolTimeout:
            st = env.get_booking_status(idempotency_key="k")["status"]
            counts["timeout_" + st] += 1
    assert 250 <= counts["confirmed"] <= 310
    assert 40 <= counts["slot_taken"] <= 80
    assert counts["timeout_booked"] > 5 and counts["timeout_not_booked"] > 5
    assert 40 <= counts["timeout_booked"] + counts["timeout_not_booked"] <= 80


def _outcomes(seed, k=15):
    env = mk(Scenario(seed=seed))
    out = []
    for i in range(k):
        s = env.search_providers("plumber", "Indiranagar", None)
        p = next(x for x in s["providers"] if x["provider_id"] == "p_plm_1")
        h = env.hold_slot("p_plm_1", p["available_slots"][0])
        try:
            out.append(env.book(h["hold_id"], f"k{i}")["status"])
        except ToolTimeout:
            out.append("timeout:" + env.get_booking_status(idempotency_key=f"k{i}")["status"])
    return out


def test_determinism_same_seed_same_outcomes_different_seed_differs():
    assert _outcomes(7) == _outcomes(7)
    assert _outcomes(7) != _outcomes(8)


def test_forced_generic_failures_any_tool():
    env = mk(Scenario().force("search_providers", 1, "transient_error").force("list_bookings", 2, "timeout"))
    with pytest.raises(TransientError):
        env.search_providers("plumber", "x", None)
    assert env.search_providers("plumber", "x", None)["status"] == "ok"
    assert env.list_bookings("u_demo")["status"] == "ok"
    with pytest.raises(ToolTimeout):
        env.list_bookings("u_demo")


def test_cancel_503_then_success_and_idempotent():
    env = mk()
    ids = [b["booking_id"] for b in env.list_bookings("u_demo")["bookings"] if b["status"] == "confirmed"]
    bid = ids[0]
    with pytest.raises(TransientError):
        env.cancel(bid, "c1")
    assert env.list_bookings("u_demo")["bookings"][-1]["status"] in ("confirmed", "cancelled")
    r = env.cancel(bid, "c1")
    assert r["status"] == "cancelled"
    assert env.cancel(bid, "c1") == r  # repeat confirm
    assert env.cancel(bid, "c2")["status"] == "not_cancellable"
    assert env.cancel("bk_nope", "c3")["status"] == "not_found"


def test_cancel_503_is_per_booking():
    env = mk(Scenario().force("book", 1, "success").force("book", 2, "success"))
    h1 = first_hold(env, idx=0)
    h2 = first_hold(env, idx=1)
    b1 = env.book(h1["hold_id"], "a")["booking_id"]
    b2 = env.book(h2["hold_id"], "b")["booking_id"]
    for bid in (b1, b2):
        with pytest.raises(TransientError):
            env.cancel(bid, "x" + bid)
        assert env.cancel(bid, "x" + bid)["status"] == "cancelled"


def test_cancel_503_can_be_disabled():
    env = mk(Scenario(cancel_transient_first=False))
    assert env.cancel("bk_seed_4", "c")["status"] == "cancelled"


def test_reschedule_conflict_and_success_idempotent():
    env = mk()
    p = next(x for x in env.search_providers("ac_servicing", "Koramangala", None)["providers"] if x["provider_id"] == "p_ac_2")
    s1, s2 = p["available_slots"][0], p["available_slots"][1]
    env.scenario.conflict_slots.add(s1)
    c = env.reschedule("bk_seed_4", s1, "r1")
    assert c["status"] == "slot_conflict" and c["alternatives"] and s1 not in c["alternatives"]
    assert env.reschedule("bk_seed_4", s1, "r1") == c
    ok = env.reschedule("bk_seed_4", s2, "r2")
    assert ok["status"] == "rescheduled" and ok["previous_slot"].endswith("12:30")
    assert env.reschedule("bk_seed_4", s2, "r2") == ok
    bk = next(b for b in env.list_bookings("u_demo")["bookings"] if b["booking_id"] == "bk_seed_4")
    assert bk["slot"] == s2
    assert env.reschedule("bk_seed_1", s2, "r3")["status"] == "not_reschedulable"


def test_reschedule_forced():
    env = mk(Scenario().force("reschedule", 1, "slot_conflict"))
    p = env.search_providers("ac_servicing", "Koramangala", None)["providers"][0]
    assert env.reschedule("bk_seed_4", p["available_slots"][0], "r")["status"] == "slot_conflict"


def test_list_bookings_seed():
    env = mk()
    bs = env.list_bookings("u_demo")["bookings"]
    assert any(b["service"] == "electrician" and b["status"] == "completed" and b["provider_id"] == "p_elc_1" for b in bs)
    up = [b for b in bs if b["service"] == "ac_servicing" and b["status"] == "confirmed"]
    assert len(up) == 1 and up[0]["slot"] > env.clock.iso()
    assert {"booking_id", "provider_id", "provider_name", "service", "slot", "status"} <= set(bs[0])
    assert env.list_bookings("someone_else")["bookings"] == []


def test_booking_appears_in_list_and_blocks_slot():
    env = mk(Scenario().force("book", 1, "success"))
    h = first_hold(env)
    env.book(h["hold_id"], "k")
    assert any(b["slot"] == h["slot"] and b["status"] == "confirmed" for b in env.list_bookings("u_demo")["bookings"])
    assert env.hold_slot("p_plm_1", h["slot"])["status"] == "slot_unavailable"


def test_persistence_across_restart(tmp_path):
    path = tmp_path / "m.db"
    clock = FakeClock()
    env = mk(Scenario().force("book", 1, "timeout_booked"), conn=db.connect(path), clock=clock)
    h = first_hold(env)
    with pytest.raises(ToolTimeout):
        env.book(h["hold_id"], "k")
    env.conn.close()
    env2 = mk(Scenario(), conn=db.connect(path), clock=clock)  # "restart"
    st = env2.get_booking_status(idempotency_key="k")
    assert st["status"] == "booked"
    assert env2.book(h["hold_id"], "k")["booking_id"] == st["booking_id"]
    assert env2.call_count("book") == 2  # call counters persisted
    assert sum(1 for b in env2.list_bookings("u_demo")["bookings"] if b["booking_id"] == st["booking_id"]) == 1
    h2 = env2.hold_slot("p_plm_1", env2.search_providers("plumber", "x", None)["providers"][0]["available_slots"][5])
    assert h2["hold_id"] != h["hold_id"]


def test_event_queue():
    env = mk()
    assert env.poll_events() == []
    ev = env.emit_provider_cancellation("bk_seed_4")
    assert ev["type"] == "provider_cancelled"
    evs = env.poll_events()
    assert len(evs) == 1 and evs[0]["booking_id"] == "bk_seed_4" and evs[0]["provider_id"] == "p_ac_2"
    assert env.poll_events() == []
    bk = next(b for b in env.list_bookings("u_demo")["bookings"] if b["booking_id"] == "bk_seed_4")
    assert bk["status"] == "cancelled_by_provider"


def test_schemas_match_tools_and_dispatch():
    assert [s["name"] for s in TOOL_SCHEMAS] == TOOL_NAMES
    for s in TOOL_SCHEMAS:
        assert s["input_schema"]["type"] == "object" and s["description"]
        assert set(s["input_schema"]["required"]) <= set(s["input_schema"]["properties"])
    env = mk()
    assert env.call("list_bookings", user_id="u_demo")["status"] == "ok"


def test_fake_clock():
    c = FakeClock()
    t0 = c.now()
    c.advance(minutes=5)
    assert (c.now() - t0).total_seconds() == 300
