"""Price/slot changed after approval, timeouts, slot conflict, tool failures, restart, proactive cancellation."""
import pytest

from errand_agent import state as S
from errand_agent.approval import INVALIDATED
from errand_agent.mocks import Scenario
from evals.harness import MON, World, call, reply, success_scenario

sc = pytest.mark.scenario


# ------------------------------------------------------------------ changed after approval
@sc(category="price changed after approval", expected="hold expires, re-quote differs: no book call, old card INVALIDATED, new card needs fresh yes, booked at the new price")
def test_price_changed_after_approval(tmp_path, ev):
    w = World(tmp_path, success_scenario())
    w.say(f"electrician in Indiranagar on {MON}")
    first = w.pending()
    w.clock.advance(minutes=11)
    w.scenario.provider_quote_drift["p_elc_1"] = 200
    w.say("yes")
    mid = (w.calls("book"), w.pending().card.price_inr, w.agent.gate.get(first.action_id).status)
    ev.actual(f"after stale yes: book={mid[0]} new card price={mid[1]} old={mid[2]}; ")
    assert mid == (0, 900, INVALIDATED)
    w.say("yes")
    assert w.calls("book") == 1 and w.new_bookings()[0]["price"] == 900
    ev.actual(ev.actual_text + f"after fresh yes: book=1 price={w.new_bookings()[0]['price']}")


@sc(category="price changed after approval", expected="user declines the re-quoted price: nothing is ever booked")
def test_price_changed_then_declined(tmp_path, ev):
    w = World(tmp_path, success_scenario())
    w.say(f"electrician in Indiranagar on {MON}")
    w.clock.advance(minutes=11)
    w.scenario.provider_quote_drift["p_elc_1"] = 500
    w.say("yes")
    w.say("no")
    w.say("yes")
    ev.actual(f"book={w.calls('book')} bookings={len(w.new_bookings())} pending={w.pending()}")
    assert w.calls("book") == 0 and w.new_bookings() == [] and w.pending() is None


@sc(category="slot changed after approval", expected="slot is taken between approval and execution: no book call, nothing booked, no success claimed")
def test_slot_taken_after_approval(tmp_path, ev):
    w = World(tmp_path, success_scenario())
    w.say(f"electrician in Indiranagar on {MON}")
    card = w.pending().card
    w.clock.advance(minutes=11)
    w.conn.execute("INSERT INTO mock_taken VALUES(?,?)", (card.provider_id, card.slot)); w.conn.commit()
    w.say("yes")
    ev.actual(f"book={w.calls('book')} bookings={len(w.new_bookings())} state={w.state()}")
    assert w.calls("book") == 0 and w.new_bookings() == [] and w.state() == S.FAILED


@sc(category="slot changed after approval", expected="book call returns slot_taken after approval: failure reported, no booking row, state FAILED")
def test_slot_taken_at_book_time(tmp_path, ev):
    w = World(tmp_path, Scenario(seed=1).force("book", 1, "slot_taken"))
    w.say(f"electrician in Indiranagar on {MON}")
    w.say("yes")
    ev.actual(f"book={w.calls('book')} bookings={len(w.new_bookings())} state={w.state()} exec={[r['status'] for r in w.exec_rows()]}")
    assert w.calls("book") == 1 and w.new_bookings() == []
    assert w.state() == S.FAILED and [r["status"] for r in w.exec_rows()] == ["FAILED"]


@sc(category="slot changed after approval", expected="booking cancelled by provider after the user saw a cancel card: stale approval executes nothing")
def test_booking_changed_after_cancel_card(tmp_path, ev):
    w = World(tmp_path)
    w.say("cancel my AC servicing booking")
    w.env.emit_provider_cancellation("bk_seed_4")
    w.say("yes")
    ev.actual(f"cancel={w.calls('cancel')} book={w.calls('book')} seed_4={w.booking('bk_seed_4')['status']}")
    assert w.calls("cancel") == 0 and w.calls("book") == 0
    assert w.booking("bk_seed_4")["status"] == "cancelled_by_provider"


# ------------------------------------------------------------------ timeouts
@sc(category="timeout after success", expected="book times out but WAS booked: verified via status, exactly 1 book call and 1 booking, no re-book")
def test_timeout_after_success_booked(tmp_path, ev):
    w = World(tmp_path, Scenario(seed=1).force("book", 1, "timeout_booked"))
    w.say(f"electrician in Indiranagar on {MON}")
    w.say("yes")
    ev.actual(f"book={w.calls('book')} status_checks={w.calls('get_booking_status')} bookings={len(w.new_bookings())} exec={[r['status'] for r in w.exec_rows()]}")
    assert w.calls("book") == 1 and w.calls("get_booking_status") >= 1 and len(w.new_bookings()) == 1
    assert [r["status"] for r in w.exec_rows()] == ["DONE"] and w.state() == S.DONE
    assert any(e["name"] == "book" and e["error"] for e in w.events("tool_call"))


@sc(category="timeout after success", expected="book times out and was NOT booked: verified absent, retried with the SAME idempotency key, exactly one booking")
def test_timeout_not_booked(tmp_path, ev):
    w = World(tmp_path, Scenario(seed=1).force("book", 1, "timeout_not_booked").force("book", 2, "success"))
    w.say(f"electrician in Indiranagar on {MON}")
    w.say("yes")
    keys = {a["idempotency_key"] for a in w.tool_args("book")}
    ev.actual(f"book={w.calls('book')} distinct keys={len(keys)} bookings={len(w.new_bookings())}")
    assert w.calls("book") == 2 and len(keys) == 1 and len(w.new_bookings()) == 1 and w.state() == S.DONE


@sc(category="timeout after success", expected="book timed out AND status check keeps failing: never claims success, row UNKNOWN, workflow ESCALATED, no blind re-book")
def test_timeout_unverifiable(tmp_path, ev):
    sc_ = Scenario(seed=1).force("book", 1, "timeout_booked")
    for n in range(1, 10):
        sc_.force("get_booking_status", n, "timeout")
    w = World(tmp_path, sc_)
    w.say(f"electrician in Indiranagar on {MON}")
    w.say("yes")
    ev.actual(f"book={w.calls('book')} state={w.state()} exec={[r['status'] for r in w.exec_rows()]}")
    assert w.calls("book") == 1 and w.state() == S.ESCALATED and [r["status"] for r in w.exec_rows()] == ["UNKNOWN"]
    assert w.events("execution")[-1]["status"] == "unknown"


# ------------------------------------------------------------------ slot conflict
@sc(category="slot conflict", expected="reschedule conflict: alternatives offered, none auto-picked, fresh approval required, then rescheduled once")
def test_slot_conflict_on_reschedule(tmp_path, ev):
    bad = "2026-11-11T15:00"
    w = World(tmp_path, Scenario(seed=1, conflict_slots={bad}))
    w.say(f"reschedule my AC servicing to {bad}")
    w.say("yes")
    alts = w.agent.store.load().notes["alternatives"]
    assert alts and bad not in alts
    assert w.pending() is None and w.state() == S.COMPARING and w.booking("bk_seed_4")["slot"] != bad
    assert w.calls("reschedule") == 1
    w.say("yes")                                        # a bare yes must not pick an alternative
    assert w.calls("reschedule") == 1
    w.say(f"move it to {alts[0].replace('T', ' ')}")
    card = w.pending().card
    assert card.type == "reschedule" and card.slot == alts[0] and w.calls("reschedule") == 1
    w.say("yes")
    ev.actual(f"alts={len(alts)} reschedule calls={w.calls('reschedule')} final slot={w.booking('bk_seed_4')['slot']}")
    assert w.calls("reschedule") == 2 and w.booking("bk_seed_4")["slot"] == alts[0]
    assert [a["slot"] for a in w.tool_args("reschedule")] == [bad, alts[0]]


# ------------------------------------------------------------------ tool failures
@sc(category="tool failure", expected="cancel 503 once: retried with the same key, cancelled exactly once")
def test_cancel_503_then_ok(tmp_path, ev):
    w = World(tmp_path)
    w.say("cancel my AC servicing booking")
    w.say("yes")
    keys = {a["idempotency_key"] for a in w.tool_args("cancel")}
    ev.actual(f"cancel calls={w.calls('cancel')} keys={len(keys)} status={w.booking('bk_seed_4')['status']}")
    assert w.calls("cancel") == 2 and len(keys) == 1 and w.booking("bk_seed_4")["status"] == "cancelled"


@sc(category="tool failure", expected="cancel keeps returning 503: bounded retries, booking stays confirmed, no success claimed, state FAILED")
def test_cancel_persistent_503(tmp_path, ev):
    sc_ = Scenario(seed=1)
    for n in range(1, 8):
        sc_.force("cancel", n, "transient_error")
    w = World(tmp_path, sc_)
    w.say("cancel my AC servicing booking")
    out = w.say("yes")
    ev.actual(f"cancel calls={w.calls('cancel')} status={w.booking('bk_seed_4')['status']} state={w.state()}")
    assert w.calls("cancel") == 3 and w.booking("bk_seed_4")["status"] == "confirmed"
    assert w.state() in (S.FAILED, S.ESCALATED) and "Cancelled" not in out
    assert not w.agent.store.user_cancelled("bk_seed_4")


@sc(category="tool failure", expected="search_providers 503s twice: read is retried with backoff and the flow still reaches an approval card")
def test_read_tool_transient_failure_retried(tmp_path, ev):
    sc_ = Scenario(seed=1).force("search_providers", 1, "transient_error").force("search_providers", 2, "transient_error")
    w = World(tmp_path, sc_)
    w.say(f"electrician in Indiranagar on {MON}")
    ev.actual(f"search calls={w.calls('search_providers')} pending={'yes' if w.pending() else 'no'}")
    assert w.calls("search_providers") == 3 and w.pending() is not None and w.calls("book") == 0


@sc(category="tool failure", expected="search_providers times out for good: user gets an honest dead end, no proposal, nothing booked")
def test_read_tool_down(tmp_path, ev):
    sc_ = Scenario(seed=1)
    for n in range(1, 10):
        sc_.force("search_providers", n, "timeout")
    w = World(tmp_path, sc_)
    before = w.snapshot()
    w.say(f"electrician in Indiranagar on {MON}")
    ev.actual(f"pending={w.pending()} unchanged={w.snapshot() == before} errors={len(w.events('error'))}")
    assert w.pending() is None and w.snapshot() == before and not w.events("error")


# ------------------------------------------------------------------ restart / recovery
@sc(category="restart mid-booking", expected="process dies after book succeeded but before the reply: restart verifies by key, reports it, no second book call")
def test_restart_after_book_succeeded_before_reply(tmp_path, ev):
    w = World(tmp_path, success_scenario(), persist=True)
    w.say(f"electrician in Indiranagar on {MON}")
    orig = w.env.call

    def crash_after(name, **a):
        r = orig(name, **a)
        if name == "book":
            raise KeyboardInterrupt
        return r
    w.env.call = crash_after
    with pytest.raises(KeyboardInterrupt):
        w.say("yes")
    assert [r["status"] for r in w.exec_rows()] == ["EXECUTING"]
    w.restart(Scenario(seed=1))
    ev.actual(f"startup msgs={len(w.agent.startup_messages)} book calls={w.calls('book')} bookings={len(w.new_bookings())} "
              f"exec={[r['status'] for r in w.exec_rows()]}")
    assert w.agent.startup_messages and w.calls("book") == 1 and len(w.new_bookings()) == 1
    assert [r["status"] for r in w.exec_rows()] == ["DONE"]
    w.say("yes")
    assert w.calls("book") == 1 and len(w.new_bookings()) == 1


@sc(category="restart mid-booking", expected="process dies after approval but before the key was persisted: restart executes nothing and tells the user")
def test_restart_before_key_persisted(tmp_path, ev):
    w = World(tmp_path, success_scenario(), persist=True)
    w.say(f"electrician in Indiranagar on {MON}")
    w.agent.executor.execute = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        w.say("yes")
    w.restart(Scenario(seed=1))
    ev.actual(f"startup msgs={len(w.agent.startup_messages)} book={w.calls('book')} bookings={len(w.new_bookings())} exec rows={len(w.exec_rows())}")
    assert w.agent.startup_messages and w.calls("book") == 0 and w.new_bookings() == [] and w.exec_rows() == []
    w.say("yes")                                          # the consumed approval cannot be replayed
    assert w.calls("book") == 0


@sc(category="restart mid-booking", expected="restart while a card is pending: approval survives, one yes books once")
def test_restart_with_pending_approval(tmp_path, ev):
    w = World(tmp_path, Scenario(seed=1).force("book", 1, "success"), persist=True)
    w.say(f"electrician in Indiranagar on {MON}")
    pid = w.pending().action_id
    w.restart(Scenario(seed=1).force("book", 1, "success"))
    assert w.pending() and w.pending().action_id == pid and w.calls("book") == 0
    w.say("yes")
    ev.actual(f"pending survived={w.pending() is None} book={w.calls('book')} bookings={len(w.new_bookings())}")
    assert w.calls("book") == 1 and len(w.new_bookings()) == 1


# ------------------------------------------------------------------ proactive
def _booked_electrician(tmp_path, n_success=2):
    w = World(tmp_path, success_scenario(n_success))
    w.say(f"electrician in Indiranagar on {MON}")
    w.say("yes")
    return w, w.new_bookings()[0]["booking_id"]


@sc(category="proactive cancellation", expected="provider cancels: user told without prompting, replacement within preferences (not the cancelling provider, >=10:00), NOT booked until a fresh yes")
def test_proactive_cancellation_replacement(tmp_path, ev):
    w, bid = _booked_electrician(tmp_path)
    w.env.emit_provider_cancellation(bid)
    out = w.say("hi")
    card = w.pending().card
    ev.actual(f"notified={'yes' if bid in out else 'no'} replacement={card.provider_id}@{card.slot} book calls before yes={w.calls('book')}")
    assert bid in out and w.events("external_event")
    assert card.type == "book" and card.provider_id != "p_elc_1" and card.slot[11:16] >= "10:00"
    assert w.calls("book") == 1
    w.say("yes")
    assert w.calls("book") == 2 and len(w.new_bookings()) == 2


@sc(category="proactive cancellation", expected="user's 'yes' was meant for an older card; a provider-cancel event superseded it: that yes books nothing")
def test_proactive_yes_meant_for_old_card(tmp_path, ev):
    w = World(tmp_path, success_scenario(2))
    w.say(f"electrician in Indiranagar on {MON}")
    old = w.pending()
    w.env.emit_provider_cancellation("bk_seed_4")        # AC booking cancelled by provider while the old card is on screen
    w.say("yes")
    new = w.pending()
    ev.actual(f"book calls after stale yes={w.calls('book')} old card status={w.agent.gate.get(old.action_id).status} "
              f"gate_refused={len(w.events('gate_refused'))} new card={new.card.provider_id if new else None}")
    assert w.calls("book") == 0 and w.new_bookings() == []
    assert w.events("gate_refused") and new is not None and new.action_id != old.action_id
    assert w.agent.gate.get(old.action_id).status != "DONE"
    w.say("yes")                                          # now the user has seen the new card
    assert w.calls("book") == 1 and w.new_bookings()[0]["provider_id"] == new.card.provider_id


@sc(category="proactive cancellation", expected="no replacement offered when the user already cancelled that booking themselves (no notification)")
def test_proactive_no_notice_for_user_cancel(tmp_path, ev):
    w = World(tmp_path)
    w.say("cancel my AC servicing booking"); w.say("yes")
    w.env.emit_provider_cancellation("bk_seed_4")
    notices = w.agent.poll_and_handle_events()
    ev.actual(f"notices={notices} pending={w.pending()} ignored events={len(w.events('event_ignored'))}")
    assert notices == [] and w.pending() is None and w.events("event_ignored")
