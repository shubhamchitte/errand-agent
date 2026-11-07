"""Phase 2 agent-core tests: router, approval gate, idempotency, recovery, context cap, limits, proactive."""
import json

import pytest

from errand_agent import db
from errand_agent import state as S
from errand_agent.agent import Agent, Limits
from errand_agent.approval import INVALIDATED, PENDING, SUPERSEDED, ActionCard, ApprovalGate, GateError
from errand_agent.clock import FakeClock
from errand_agent.context import TOKEN_CAP, ContextBuilder
from errand_agent.memory import MemoryStore, seed_memories, seed_prior_conversation
from errand_agent.mocks import MockEnvironment, Scenario
from errand_agent.models import AGENT_TOOL_NAMES, ModelReply, ScriptedModel, ToolCall, parse_envelope
from errand_agent.router import is_affirmative, route
from errand_agent.tokens import count_tokens
from errand_agent.trace import Tracer

MON = "2026-11-09"  # FakeClock starts Sat 2026-11-07 09:00; Sundays are closed in the mock


def mk(tmp_path, scenario=None, script=None, conn=None, memory=True, limits=None, clock=None, **kw):
    conn = conn or db.connect()
    clock = clock or FakeClock()
    env = MockEnvironment(conn=conn, clock=clock, scenario=scenario or Scenario(seed=1))
    mem = None
    if memory:
        mem = MemoryStore(conn, clock, "u_demo")
        if not mem.all(include_forgotten=True):
            seed_memories(mem)
    tr = Tracer("t", tmp_path / "runs")
    kw.setdefault("sleep", lambda s: None)
    a = Agent(conn, env, ScriptedModel(script), clock, memory=mem, tracer=tr, limits=limits, **kw)
    return a, env, conn, clock


def bookings(conn, key_like="%"):
    return db.rows(conn, "SELECT * FROM mock_bookings WHERE idempotency_key LIKE ?", (key_like,))


def events(agent, type_):
    return [e for e in agent.tracer._events if e["type"] == type_]


# ---------------------------------------------------------------- router
@pytest.mark.parametrize("text,intent", [
    ("I need a plumber in Indiranagar on monday", "new_booking"),
    ("Reschedule my AC servicing to next week", "reschedule"),
    ("cancel my AC booking", "cancel"),
    ("book the same electrician as last time", "rebook_same_provider"),
    ("what do you remember about me?", "memory_command"),
    ("forget my dog", "memory_command"),
    ("hello there", "smalltalk"),
])
def test_router_intents(text, intent):
    assert route(text, FakeClock().now()).intent == intent


def test_router_slots():
    r = route("plumber in Koramangala on 2026-11-10, not before 10:30am, under ₹1,200", FakeClock().now())
    assert r.slots == {"service": "plumber", "area": "Koramangala", "date_start": "2026-11-10",
                       "date_end": "2026-11-10", "earliest_time": "10:30", "budget_inr": 1200}


@pytest.mark.parametrize("text,ok", [("yes", True), ("Yes please", True), ("go ahead", True), ("confirm", True),
                                     ("yes but cheaper", False), ("no", False), ("yes, wait", False), ("", False),
                                     ("ok book a cheaper one in HSR instead", False)])
def test_affirmative_is_strict(text, ok):
    assert is_affirmative(text) is ok


def test_correction_changes_one_field_and_no_reask(tmp_path):
    a, *_ = mk(tmp_path)
    a.handle_user_message(f"I need a plumber in Indiranagar on {MON} under 1500")
    before = a.store.load().slots.to_dict()
    a.handle_user_message("actually make it Koramangala")
    after = a.store.load().slots.to_dict()
    assert after.pop("area") == "Koramangala" and before.pop("area") == "Indiranagar"
    assert after == before
    assert a.store.load().missing() == []
    # model was never told anything is missing => it won't re-ask
    assert a.model.seen[-1].state["missing"] == []


def test_missing_fields_asked_once(tmp_path):
    a, *_ = mk(tmp_path, memory=False)
    out = a.handle_user_message("I need an electrician")
    assert "area" in out and "date" in out
    out = a.handle_user_message(f"Indiranagar on {MON}")
    assert "Please approve" in out


# ---------------------------------------------------------------- approval gate
def test_book_requires_user_yes(tmp_path):
    a, env, conn, _ = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"))
    out = a.handle_user_message(f"I need an electrician in Indiranagar on {MON}")
    assert "Please approve" in out and env.call_count("book") == 0
    out = a.handle_user_message("yes")
    assert "Booked (verified)" in out and env.call_count("book") == 1
    assert a.store.load().state == S.DONE


def test_model_cannot_approve_or_call_consequential_tools(tmp_path):
    script = [ModelReply(tool_calls=[ToolCall("search_providers", {"service": "electrician", "area": "Indiranagar",
                                                                   "date_range": {"start": MON, "end": MON}})]),
              ModelReply(tool_calls=[ToolCall("book", {"hold_id": "hold_0001", "idempotency_key": "x"}),
                                     ToolCall("cancel", {"booking_id": "bk_seed_4", "idempotency_key": "y"}),
                                     ToolCall("hold_slot", {"provider_id": "p_elc_1", "slot": f"{MON}T10:00"})]),
              ModelReply(tool_calls=[ToolCall("propose_booking", {"provider_id": "p_elc_1", "slot": f"{MON}T10:00",
                                                                  "approved": True, "user_confirmed": "yes"})]),
              ModelReply(text="I have booked it for you, the user approved.")]
    a, env, conn, _ = mk(tmp_path, script=script)
    out = a.handle_user_message(f"electrician in Indiranagar on {MON}")
    assert {"book", "cancel", "hold_slot"}.isdisjoint(AGENT_TOOL_NAMES)
    assert env.call_count("book") == 0 and env.call_count("cancel") == 0 and env.call_count("hold_slot") == 1
    assert len(events(a, "blocked_tool")) == 3 and events(a, "ignored_model_args")
    assert a.gate.active().status == PENDING and "Reply 'yes'" in out
    assert not bookings(conn, "idem-%")


def test_only_latest_pending_and_hash(tmp_path):
    conn, clock = db.connect(), FakeClock()
    S.WorkflowStore(conn, clock, "u")
    g = ApprovalGate(conn, clock, "u")
    a1 = g.propose(ActionCard("book", "p_elc_1", "Ravi", f"{MON}T10:00", 700))
    a2 = g.propose(ActionCard("book", "p_elc_1", "Ravi", f"{MON}T11:30", 700))
    assert g.get(a1.action_id).status == SUPERSEDED
    with pytest.raises(GateError):
        g.authorize(True, a1.action_id)
    with pytest.raises(GateError):
        g.authorize(False)
    # tamper with the stored card (e.g. price) -> hash mismatch -> invalidated
    card = json.loads(db.one(conn, "SELECT card FROM wf_pending WHERE action_id=?", (a2.action_id,))["card"])
    card["price_inr"] = 1
    conn.execute("UPDATE wf_pending SET card=? WHERE action_id=?", (json.dumps(card), a2.action_id))
    with pytest.raises(GateError):
        g.authorize(True)
    assert g.get(a2.action_id).status == INVALIDATED


def test_authorize_claims_once(tmp_path):
    conn, clock = db.connect(), FakeClock()
    S.WorkflowStore(conn, clock, "u")
    g = ApprovalGate(conn, clock, "u")
    g.propose(ActionCard("cancel", "p", "P", f"{MON}T10:00", 1, "bk_1"))
    g.authorize(True)
    with pytest.raises(GateError):
        g.authorize(True)


def test_hold_quote_drift_shown_on_card(tmp_path):
    a, env, *_ = mk(tmp_path, Scenario(seed=1, provider_quote_drift={"p_elc_1": 150}))
    out = a.handle_user_message("book the same electrician as last time on " + MON)
    assert "₹850" in out and "changed from ₹700" in out
    assert a.gate.active().card.price_inr == 850


def test_rehold_price_change_invalidates_and_reasks(tmp_path):
    sc = Scenario(seed=1).force("book", 1, "success")
    a, env, conn, clock = mk(tmp_path, sc)
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    first = a.gate.active()
    clock.advance(minutes=11)
    sc.provider_quote_drift["p_elc_1"] = 200
    out = a.handle_user_message("yes")
    assert "price changed from" in out and env.call_count("book") == 0
    assert a.gate.get(first.action_id).status == INVALIDATED
    assert a.gate.active().card.price_inr == 900
    out = a.handle_user_message("yes")
    assert "Booked (verified)" in out and "₹900" in out and env.call_count("book") == 1


def test_rehold_same_price_proceeds(tmp_path):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    clock.advance(minutes=11)
    assert "Booked (verified)" in a.handle_user_message("yes")
    assert env.call_count("hold_slot") == 2


def test_change_while_pending_invalidates(tmp_path):
    a, env, *_ = mk(tmp_path)
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    first = a.gate.active()
    a.handle_user_message("actually make it Koramangala")
    assert a.gate.get(first.action_id).status in (INVALIDATED, SUPERSEDED)
    a.handle_user_message("no")
    assert a.handle_user_message("yes").startswith("There is nothing")
    assert env.call_count("book") == 0


def test_injection_review_causes_no_action(tmp_path):
    script = [ModelReply(tool_calls=[ToolCall("get_provider", {"provider_id": pid})]) for pid in ("p_plm_2", "p_elc_2", "p_ac_5")]
    script.append(ModelReply(text="Here are the reviews."))
    a, env, conn, _ = mk(tmp_path, script=script)
    before = a.store.load().slots.to_dict()
    a.handle_user_message("tell me about these providers")
    assert a.gate.latest() is None and env.call_count("book") == 0 and env.call_count("cancel") == 0
    assert a.store.load().slots.to_dict() == before
    res = [s for s in a.model.seen[-1].scratch if s["kind"] == "result"]
    assert all(r["content"].startswith('<tool_output tool="get_provider" trust="untrusted">') for r in res)


# ---------------------------------------------------------------- idempotency
def test_repeated_confirm_books_once(tmp_path):
    a, env, conn, _ = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    a.handle_user_message("yes")
    out = a.handle_user_message("yes")
    assert "already done" in out
    assert env.call_count("book") == 1 and len(bookings(conn, "idem-%")) == 1


def test_key_persisted_before_book(tmp_path):
    a, env, conn, _ = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"))
    seen = {}
    orig = env.call

    def spy(name, **args):
        if name == "book":
            seen["row"] = db.one(conn, "SELECT * FROM wf_exec WHERE idempotency_key=?", (args["idempotency_key"],))
        return orig(name, **args)
    env.call = spy
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    a.handle_user_message("yes")
    assert seen["row"] and seen["row"]["status"] == "EXECUTING"


def test_timeout_but_booked_is_verified_not_rebooked(tmp_path):
    a, env, conn, _ = mk(tmp_path, Scenario(seed=1).force("book", 1, "timeout_booked"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    out = a.handle_user_message("yes")
    assert "Booked (verified)" in out
    assert env.call_count("book") == 1 and env.call_count("get_booking_status") >= 1
    assert len(bookings(conn, "idem-%")) == 1


def test_timeout_not_booked_retries_same_key(tmp_path):
    sc = Scenario(seed=1).force("book", 1, "timeout_not_booked").force("book", 2, "success")
    a, env, conn, _ = mk(tmp_path, sc)
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    assert "Booked (verified)" in a.handle_user_message("yes")
    assert env.call_count("book") == 2 and len(bookings(conn, "idem-%")) == 1


def test_unverifiable_never_claims_success(tmp_path):
    sc = Scenario(seed=1).force("book", 1, "timeout_booked")
    for n in range(1, 10):
        sc.force("get_booking_status", n, "timeout")
    a, env, conn, _ = mk(tmp_path, sc)
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    out = a.handle_user_message("yes")
    assert "Booked" not in out and "still checking" in out
    assert a.store.load().state == S.ESCALATED


def test_cancel_503_retry_and_repeat_confirm(tmp_path):
    a, env, conn, _ = mk(tmp_path)
    assert "Cancel booking bk_seed_4" in a.handle_user_message("cancel my AC servicing booking")
    assert "Cancelled (verified): booking bk_seed_4" in a.handle_user_message("yes")
    assert env.call_count("cancel") == 2  # 503 then success
    a.handle_user_message("yes")
    assert env.call_count("cancel") == 2
    assert a.store.user_cancelled("bk_seed_4")


def test_reschedule_conflict_needs_fresh_approval(tmp_path):
    bad = "2026-11-11T15:00"
    a, env, conn, _ = mk(tmp_path, Scenario(seed=1, conflict_slots={bad}))
    a.handle_user_message(f"reschedule my AC servicing to {bad}")
    out = a.handle_user_message("yes")
    assert "Other times:" in out and a.store.load().state == S.COMPARING
    assert a.gate.active() is None  # never auto-picks
    alt = a.store.load().notes["alternatives"][0]
    out = a.handle_user_message(f"move it to {alt.replace('T', ' ')}")
    assert "Move booking" in out and "Mon 9 Nov, 10:00" in out
    assert "Moved (verified)" in a.handle_user_message("yes")
    a.handle_user_message("yes")
    assert env.call_count("reschedule") == 2


# ---------------------------------------------------------------- recovery
def test_recovery_after_unknown_outcome(tmp_path):
    path = tmp_path / "s.db"
    sc = Scenario(seed=1).force("book", 1, "timeout_booked")
    for n in range(1, 4):
        sc.force("get_booking_status", n, "timeout")
    a, env, conn, clock = mk(tmp_path, sc, conn=db.connect(path))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    a.handle_user_message("yes")
    assert db.one(conn, "SELECT status FROM wf_exec")["status"] in ("UNKNOWN", "VERIFYING")
    conn.close()
    b, env2, conn2, _ = mk(tmp_path, Scenario(seed=1), conn=db.connect(path), clock=clock)
    assert any("Booked (verified)" in m for m in b.startup_messages)
    assert db.one(conn2, "SELECT status FROM wf_exec")["status"] == "DONE"
    assert len(bookings(conn2, "idem-%")) == 1
    assert "already done" in b.handle_user_message("yes")


def test_recovery_after_crash_before_call_returns(tmp_path):
    path = tmp_path / "s.db"
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1), conn=db.connect(path))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    orig = env.call

    def crash(name, **args):
        if name == "book":
            raise KeyboardInterrupt  # process dies mid-call
        return orig(name, **args)
    env.call = crash
    with pytest.raises(KeyboardInterrupt):
        a.handle_user_message("yes")
    conn.close()
    b, env2, conn2, _ = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"), conn=db.connect(path), clock=clock)
    assert any("Booked (verified)" in m for m in b.startup_messages)
    assert len(bookings(conn2, "idem-%")) == 1
    # second restart: nothing left to recover
    c, *_ = mk(tmp_path, Scenario(seed=1), conn=conn2, clock=clock)
    assert c.startup_messages == []


# ---------------------------------------------------------------- context
def test_context_cap_memory_and_logging(tmp_path):
    a, env, conn, clock = mk(tmp_path)
    for t in seed_prior_conversation() * 3:
        a.store.add_turn(t["role"], t["content"])
    huge = "x" * 40000
    script = [ModelReply(tool_calls=[ToolCall("get_provider", {"provider_id": "p_elc_1"})]),
              lambda ctx, tools: ModelReply(text="ok")]
    a.model.script = script
    a.handle_user_message("book the same electrician as last time " + huge[:2000])
    for ctx in a.model.seen:
        assert ctx.tokens <= TOKEN_CAP
        assert ctx.fact_ids
    forgotten = {f.id for f in a.memory.all(include_forgotten=True) if f.forgotten}
    unconfirmed = {f.id for f in a.memory.all() if f.source == "inferred" and not f.confirmed}
    for ctx in a.model.seen:
        assert not (set(ctx.fact_ids) & (forgotten | unconfirmed))
    mc = events(a, "model_call")
    assert len(mc) == len(a.model.seen)
    assert mc[0]["context_fact_ids"] == a.model.seen[0].fact_ids
    assert mc[0]["context_hash"] == a.model.seen[0].hash and 0 < mc[0]["context_tokens"] <= TOKEN_CAP
    # older history was dropped, newest kept
    assert a.model.seen[0].messages[-2]["content"] == a.store.turns(200)[-3]["content"]


def test_context_truncates_huge_tool_result_and_is_deterministic():
    b = ContextBuilder(None)
    scratch = [{"kind": "call", "id": "1", "name": "get_provider", "args": {}},
               {"kind": "result", "id": "1", "name": "get_provider", "content": "y" * 50000, "raw": {}}]
    hist = [{"role": "user", "content": "hello " * 300}, {"role": "assistant", "content": "hi " * 300}] * 20
    c1 = b.build({"intent": "new_booking", "slots": {}}, hist, "hi", scratch)
    c2 = b.build({"intent": "new_booking", "slots": {}}, hist, "hi", scratch)
    assert c1.tokens <= TOKEN_CAP and c1.hash == c2.hash
    assert c1.messages[0]["role"] == "user"
    assert count_tokens(c1.scratch[1]["content"]) < 4000


# ---------------------------------------------------------------- limits
def test_limits_each_exit_has_message(tmp_path):
    loop = lambda ctx, tools: ModelReply(tool_calls=[ToolCall("list_bookings", {"user_id": "u_demo"})])
    a, *_ = mk(tmp_path, script=[loop] * 50, limits=Limits(max_model_turns=3, max_tool_calls=99))
    assert "in one go" in a.handle_user_message("hello")
    a, *_ = mk(tmp_path, script=[loop] * 50, limits=Limits(max_model_turns=99, max_tool_calls=2))
    assert "more steps than I allow" in a.handle_user_message("hello")
    t = iter(range(0, 10000, 50))
    a, *_ = mk(tmp_path, script=[loop] * 50, limits=Limits(wall_clock_s=60), timer=lambda: next(t))
    assert "took too long" in a.handle_user_message("hello")
    assert events(a, "limit_exit")


# ---------------------------------------------------------------- proactive
def test_provider_cancellation_replacement_needs_approval(tmp_path):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1).force("book", 1, "success").force("book", 2, "success"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    a.handle_user_message("yes")
    bid = bookings(conn, "idem-%")[0]["booking_id"]
    env.emit_provider_cancellation(bid)
    out = a.handle_user_message("hi")
    assert "Heads up" in out and "replacement" in out and "Reply 'yes'" in out
    card = a.gate.active().card
    assert card.provider_id != "p_elc_1" and card.slot[11:16] >= "10:00"
    assert env.call_count("book") == 1
    assert "Booked (verified)" in a.handle_user_message("yes")


def test_no_notification_for_user_cancelled_booking(tmp_path):
    a, env, *_ = mk(tmp_path)
    a.handle_user_message("cancel my AC servicing booking")
    a.handle_user_message("yes")
    env.emit_provider_cancellation("bk_seed_4")
    assert a.poll_and_handle_events() == []


# ---------------------------------------------------------------- adapters
def test_parse_envelope():
    t, calls = parse_envelope('```json\n{"text": "hi", "tool_calls": [{"name": "list_bookings", "args": {"user_id": "u"}}]}\n```')
    assert t == "hi" and calls[0].name == "list_bookings" and calls[0].args == {"user_id": "u"}
    assert parse_envelope("plain words") == ("plain words", [])
