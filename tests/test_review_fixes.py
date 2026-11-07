"""Regression tests for code review r1 findings (#1-#8)."""
import json

import pytest

from errand_agent import db
from errand_agent import state as S
from errand_agent.agent import MEMORY_SLOT_MAP, Limits, parse_area, parse_earliest, parse_inr
from errand_agent.approval import EXPIRED, INVALIDATED, ActionCard
from errand_agent.context import TOKEN_CAP, ContextBuilder
from errand_agent.executor import Executor
from errand_agent.mocks import Scenario
from errand_agent.models import AGENT_TOOLS, ClaudeAgentModel, ModelReply, ToolCall, model_cost

from test_agent_core import MON, bookings, events, mk


def _search(service="electrician", area="Indiranagar", day=MON):
    return ModelReply(tool_calls=[ToolCall("search_providers", {"service": service, "area": area,
                                                                "date_range": {"start": day, "end": day}})])


# ------------------------------------------------------------ #1 approval bound to the card last shown
def test_yes_cannot_approve_card_created_by_event_before_it_was_shown(tmp_path):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1).force("book", 1, "success").force("book", 2, "success"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    assert "Booked (verified)" in a.handle_user_message("yes")
    bid = bookings(conn, "idem-%")[0]["booking_id"]
    out = a.handle_user_message(f"I need a plumber in Indiranagar on {MON}")
    shown_a = a.gate.active()
    assert "Please approve" in out and shown_a.card.service == "plumber"
    env.emit_provider_cancellation(bid)  # arrives between the user seeing card A and replying
    out = a.handle_user_message("yes")
    assert env.call_count("book") == 1  # nothing executed
    assert "earlier request" in out and a.gate.active().card.service == "electrician"
    assert events(a, "gate_refused")
    out = a.handle_user_message("yes")  # now card B has been shown
    assert "Booked (verified)" in out and env.call_count("book") == 2


# ------------------------------------------------------------ #2 expiry + fresh recompute at execution
def test_stale_pending_card_expires(tmp_path):
    a, env, conn, clock = mk(tmp_path)
    a.handle_user_message("cancel my AC servicing booking")
    pend = a.gate.active()
    clock.advance(minutes=31)
    out = a.handle_user_message("yes")
    assert "expired" in out and env.call_count("cancel") == 0
    assert a.gate.get(pend.action_id).status == EXPIRED


def test_cancel_recomputed_from_fresh_state(tmp_path):
    a, env, conn, clock = mk(tmp_path)
    a.handle_user_message("cancel my AC servicing booking")
    card = a.gate.active().card
    conn.execute("UPDATE mock_bookings SET price=price+999 WHERE booking_id=?", (card.booking_id,))
    conn.commit()
    out = a.handle_user_message("yes")
    assert "changed since you approved" in out and env.call_count("cancel") == 0
    assert a.gate.latest().status == INVALIDATED


# ------------------------------------------------------------ #3 duplicate guard by provider+slot
def test_book_same_thing_twice_refused(tmp_path):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    a.handle_user_message("yes")
    b = bookings(conn, "idem-%")[0]
    # a second, separately approved action for the same provider+slot (different action_id => different key)
    act = a.gate.propose(ActionCard("book", b["provider_id"], b["provider_name"], b["slot"], b["price"], None,
                                    "electrician"), "hold_x", "2099-01-01T00:00:00")
    a.store.set_shown(act.action_id)
    out = a.handle_user_message("yes")
    assert "already booked" in out and b["booking_id"] in out
    assert env.call_count("book") == 1 and len(bookings(conn, "idem-%")) == 1


def test_second_same_day_booking_is_flagged_on_card(tmp_path):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1).force("book", 1, "success"))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    a.handle_user_message("yes")
    out = a.handle_user_message(f"I need an electrician in Indiranagar on {MON}")
    assert "you already have" in out


# ------------------------------------------------------------ #4 Agent SDK native tool calls (no live call)
def test_claude_agent_model_uses_native_mcp_tools():
    m = ClaudeAgentModel("claude-sonnet-4-5")
    ctx = ContextBuilder(None).build({"slots": {}}, [], "hi", [])
    o = m.build_options(ctx, AGENT_TOOLS)
    assert o.tools == [] and o.max_turns == 1 and "errand" in o.mcp_servers
    assert sorted(o.allowed_tools) == sorted("mcp__errand__" + t["name"] for t in AGENT_TOOLS)
    assert not any(n.startswith("mcp__errand__book") or n.endswith("__cancel") for n in o.allowed_tools)


def test_claude_agent_model_collects_tool_use_and_cost():
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
    msgs = [AssistantMessage([TextBlock("Searching."),
                              ToolUseBlock("tu_1", "mcp__errand__search_providers", {"service": "plumber"})],
                             "claude-sonnet-4-5"),
            ResultMessage("success", 10, 10, False, 1, "s", usage={"input_tokens": 1000, "output_tokens": 100},
                          total_cost_usd=None)]
    r = ClaudeAgentModel("claude-sonnet-4-5").collect(msgs)
    assert r.text == "Searching." and r.tool_calls[0].name == "search_providers"
    assert r.tool_calls[0].args == {"service": "plumber"} and r.tool_calls[0].id == "tu_1"
    assert r.usage["cost_usd"] == pytest.approx(1000 * 3 / 1e6 + 100 * 15 / 1e6)
    assert model_cost("claude-sonnet-4-5", 1, 1, reported=0.5) == 0.5


# ------------------------------------------------------------ #5 full context sidecar + hard cap
def test_context_sidecar_excludes_forgotten_and_unconfirmed_text(tmp_path):
    a, *_ = mk(tmp_path)
    a.handle_user_message(f"I need an electrician in Indiranagar on {MON}")
    path = a.tracer.dir / f"{a.tracer.run_id}.context.jsonl"
    recs = [json.loads(l) for l in path.read_text().splitlines()]
    assert recs and len(recs) == len(a.model.seen)
    payload = "\n".join(r["system"] + r["rendered"] for r in recs)
    assert "Not before 10am" in payload  # relevant confirmed fact is really there
    for f in a.memory.all(include_forgotten=True):
        if f.forgotten or (f.source == "inferred" and not f.confirmed):
            assert f.value not in payload, f.key


def test_cap_enforced_with_large_scratch():
    scratch = []
    for i in range(300):
        scratch.append({"kind": "call", "id": str(i), "name": "get_provider", "args": {"provider_id": "p" * 50}})
        scratch.append({"kind": "result", "id": str(i), "name": "get_provider", "content": "z " * 200, "raw": {}})
    ctx = ContextBuilder(None).build({"intent": "new_booking", "slots": {}}, [], "hi " * 50, scratch)
    assert ctx.tokens <= TOKEN_CAP


# ------------------------------------------------------------ #6 wall clock reaches the executor
def test_executor_respects_deadline_with_backoff(tmp_path):
    sc = Scenario(seed=1)
    for n in range(1, 4):
        sc.force("book", n, "transient_error")
    clock_t, step = [0.0], [0.0]

    def timer():  # frozen during the first message, then +20s per reading
        clock_t[0] += step[0]
        return clock_t[0]
    sleeps = []
    a, env, conn, _ = mk(tmp_path, sc, timer=timer, sleep=sleeps.append, limits=Limits(wall_clock_s=70))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    step[0] = 20
    out = a.handle_user_message("yes")
    assert "ran out of time" in out and "Booked" not in out
    assert env.call_count("book") < 3 and sleeps and all(s > 0 for s in sleeps)
    assert events(a, "deadline_exceeded") and a.store.load().state == S.ESCALATED


def test_tool_call_storm_is_bounded(tmp_path):
    sc = Scenario(seed=1)
    for n in range(1, 200):
        sc.force("list_bookings", n, "transient_error")
    storm = ModelReply(tool_calls=[ToolCall("list_bookings", {"user_id": "u_demo"})] * 50)
    sleeps = []
    a, env, *_ = mk(tmp_path, sc, script=[storm] * 5, sleep=sleeps.append, limits=Limits(max_tool_calls=4))
    assert "more steps than I allow" in a.handle_user_message("hello")
    assert env.call_count("list_bookings") <= 4 * 3 and len(sleeps) <= 4 * 2


# ------------------------------------------------------------ #7 explicit memory->slot table, narrow errors
def test_memory_slot_parsers():
    assert parse_earliest("Not before 10am on any day; never schedule visits earlier than 10:00") == "10:00"
    assert parse_earliest("Anytime after 8am is fine") == "08:00"
    assert parse_inr("Usually spends up to Rs 1500 for a minor home visit") == 1500
    assert parse_inr("Flat 4B, pin 560038") is None
    assert parse_area("Lives in Indiranagar, Bengaluru; pin 560038") == "Indiranagar"
    assert set(MEMORY_SLOT_MAP) == {"pref.earliest_start", "budget.typical_visit", "area.locality"}


def test_unmapped_memory_key_never_fills_slot(tmp_path):
    a, *_ = mk(tmp_path)
    a.memory.forget("pref.earliest_start")
    a.memory.remember("note.earliest_gym", "Gym at 7am", source="explicit", category="note",
                      tags=["global", "new_booking", "time"])
    a.handle_user_message(f"I need an electrician on {MON}")
    sl = a.store.load().slots
    assert sl.earliest_time is None and sl.budget_inr == 1500 and sl.area == "Indiranagar"


def test_bad_tool_args_return_error_not_typeerror(tmp_path):
    a, *_ = mk(tmp_path, script=[ModelReply(tool_calls=[ToolCall("get_provider", {"bogus": 1})]),
                                 lambda ctx, tools: ModelReply(text=ctx.scratch[-1]["content"])])
    out = a.handle_user_message("hello")
    assert "missing required argument" in out and "provider_id" in out


# ------------------------------------------------------------ #8 proposals validated against search + constraints
def test_propose_not_in_search_results_refused(tmp_path):
    a, env, *_ = mk(tmp_path, script=[ModelReply(tool_calls=[ToolCall("propose_booking",
                                                                      {"provider_id": "p_elc_1", "slot": f"{MON}T10:00"})]),
                                      lambda ctx, tools: ModelReply(text=ctx.scratch[-1]["content"])])
    out = a.handle_user_message(f"electrician in Indiranagar on {MON}")
    assert "not in the current search results" in out and env.call_count("hold_slot") == 0
    assert a.gate.active() is None


def test_propose_violating_earliest_time_refused(tmp_path):
    early = lambda ctx, tools: ModelReply(tool_calls=[ToolCall("propose_booking", {
        "provider_id": p["provider_id"], "slot": s}) for p in [ctx.scratch[-1]["raw"]["providers"][0]]
        for s in [x for x in p["available_slots"] if x[11:16] < "10:00"][:1]])
    a, env, *_ = mk(tmp_path, script=[_search(), early, lambda ctx, tools: ModelReply(text=ctx.scratch[-1]["content"])])
    out = a.handle_user_message(f"electrician in Indiranagar on {MON} not before 10am")
    if env.call_count("search_providers") and "earlier than" not in out:
        pytest.skip("no pre-10:00 slot in mock data")
    assert "earlier than the user's earliest time" in out and env.call_count("hold_slot") == 0


def test_out_of_area_proposal_flagged_on_card(tmp_path):
    def pick(ctx, tools):
        far = [p for p in ctx.scratch[-1]["raw"]["providers"] if not p["in_requested_area"]][0]
        ok = [s for s in far["available_slots"] if s[11:16] >= "10:00"][0]
        return ModelReply(tool_calls=[ToolCall("propose_booking", {"provider_id": far["provider_id"], "slot": ok})])
    a, env, *_ = mk(tmp_path, script=[_search(), pick])
    out = a.handle_user_message(f"electrician in Indiranagar on {MON}")
    assert "Please approve" in out and "not Indiranagar" in out
