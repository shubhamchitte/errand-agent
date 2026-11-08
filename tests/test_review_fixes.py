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


# ------------------------------------------------------------------ hold countdown, fingerprint, ranking, run summary
def _book_world(tmp_path):
    from evals.harness import MON, World, success_scenario
    w = World(tmp_path, success_scenario())
    w.say(f"electrician in Indiranagar on {MON}")
    return w


def test_card_shows_hold_countdown_and_fingerprint(tmp_path):
    w = _book_world(tmp_path)
    p = w.pending()
    out = w.agent.store.turns(2)[-1]["content"]
    assert "Slot held for 10 min" in out and p.card.fingerprint() in out
    assert p.card.fingerprint() == "#" + p.hash[:4]
    w.clock.advance(minutes=4)
    assert p.hold_minutes_left(w.clock) == 6


def test_rehold_when_under_two_minutes_left(tmp_path):
    w = _book_world(tmp_path)
    w.clock.advance(minutes=9, seconds=30)  # 30 s left on the hold, not yet expired
    w.say("yes")
    assert w.calls("hold_slot") == 2 and w.calls("book") == 1


def test_no_rehold_with_plenty_of_time(tmp_path):
    w = _book_world(tmp_path)
    w.clock.advance(minutes=3)
    w.say("yes")
    assert w.calls("hold_slot") == 1 and w.calls("book") == 1


def test_approval_check_event_logged_before_execution(tmp_path):
    w = _book_world(tmp_path)
    fp = w.pending().card.fingerprint()
    w.say("yes")
    evs = w.tracer._events
    i = next(i for i, e in enumerate(evs) if e["type"] == "approval_check")
    assert evs[i]["fingerprint"] == fp and evs[i]["match"] is True
    assert i < next(i for i, e in enumerate(evs) if e["type"] == "tool_call" and e["name"] == "book")


def test_ranking_event_and_run_summary(tmp_path):
    w = _book_world(tmp_path)
    r = w.events("ranking")[0]
    assert r["uses_review_text"] is False and r["scores"]
    assert {"price", "rating", "slot_fit", "prefs"} <= set(r["scores"][0])
    line = w.agent.finish()
    s = w.events("run_summary")[0]
    assert line.startswith(f"{s['ai_calls']} AI calls, {s['tool_calls']} tool calls, {s['tokens']} tokens, ₹")
    assert line.endswith(" s")


def test_ranking_logged_and_sorted_for_non_scripted_model(tmp_path):
    from errand_agent.models import ModelReply, ToolCall
    from evals.harness import MON, World
    seen = {}

    def look(ctx, tools):
        seen["ctx"] = ctx
        return ModelReply(text="ok")
    script = [ModelReply(tool_calls=[ToolCall("search_providers", {"service": "electrician", "area": "Indiranagar",
                                                                    "date_range": {"start": MON, "end": MON}})]), look]
    w = World(tmp_path, script=script)
    w.agent.model.policy = None  # no heuristic policy: only the script drives the model
    w.say(f"electrician in Indiranagar on {MON}")
    r = w.events("ranking")
    assert len(r) == 1 and r[0]["uses_review_text"] is False
    res = [x for x in seen["ctx"].render().split("provider_id")[1:]]
    order = [s.split('"')[2] for s in res]
    assert order[: len(r[0]["order"])] == r[0]["order"]


def test_forgotten_fact_scrubbed_from_history(tmp_path):
    from evals.harness import World
    w = World(tmp_path)
    w.seed_history(1)
    w.say("what do you remember about me")
    dog = next(f for f in w.mem.all() if "dog" in f.value.lower())
    assert dog.value in w.agent.store.turns(5)[-1]["content"]  # listing is in chat history
    w.say(f"forget {dog.key}")
    assert w.mem.get(dog.key).forgotten
    w.say("electrician in Indiranagar tomorrow")
    assert dog.value not in w.payload_text()


# ------------------------------------------------------------ live record-live bugs
def test_relative_week_windows():
    from datetime import date
    from errand_agent.router import _dates
    sun, wed = date(2026, 11, 8), date(2026, 11, 11)
    assert _dates("this week", sun) == ("2026-11-08", "2026-11-14")
    assert _dates("this week", wed) == ("2026-11-11", "2026-11-17")
    assert _dates("next week", sun) == ("2026-11-09", "2026-11-15")
    assert _dates("this weekend", wed) == ("2026-11-14", "2026-11-15")
    assert _dates("this weekend", sun) == ("2026-11-08", "2026-11-08")


def _fake_sdk_query(stream, exc):
    async def fake(prompt, options):
        for m in stream:
            yield m
        if exc:
            raise exc
    return fake


def test_claude_agent_model_max_turns_returns_captured_tool_calls(monkeypatch):
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
    from claude_agent_sdk._errors import ResultError
    res = ResultMessage("error_max_turns", 10, 10, True, 1, "s", usage={"input_tokens": 500, "output_tokens": 50},
                        total_cost_usd=0.01, result="Reached maximum number of turns")
    stream = [AssistantMessage([ToolUseBlock("tu_9", "mcp__errand__search_providers", {"service": "plumber"})],
                               "claude-sonnet-4-5"), res]
    err = ResultError("Claude Code returned an error result: Reached maximum number of turns",
                      data={"subtype": "error_max_turns", "result": "Reached maximum number of turns"}, exit_code=1)
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_sdk_query(stream, err))
    ctx = ContextBuilder(None).build({"slots": {}}, [], "plumber this week", [])
    r = ClaudeAgentModel("claude-sonnet-4-5").respond(ctx, AGENT_TOOLS)
    assert [c.name for c in r.tool_calls] == ["search_providers"] and r.tool_calls[0].id == "tu_9"
    assert "maximum" not in r.text and r.usage["cost_usd"] == 0.01


def test_claude_agent_model_other_errors_still_raise(monkeypatch):
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_sdk_query([], RuntimeError("boom")))
    ctx = ContextBuilder(None).build({"slots": {}}, [], "hi", [])
    with pytest.raises(RuntimeError):
        ClaudeAgentModel("claude-sonnet-4-5").respond(ctx, AGENT_TOOLS)


def test_chat_pane_strips_markdown():
    from errand_agent.ui import chat_lines
    out = "".join(t.plain for t in chat_lines([("agent", "## Hi\n**Bob** is `ok`")], 60, 20))
    assert "**" not in out and "`" not in out and "#" not in out and "Bob is ok" in out


# ------------------------------------------------------------ run_f8c4c302 cosmetic fixes
@pytest.mark.parametrize("word", ["tomorrow", "tommorow", "tomorow", "tmrw", "tmr", "tomorrow,"])
def test_tomorrow_misspellings_route_to_the_date(word):
    from datetime import datetime
    from errand_agent.router import route
    now = datetime(2026, 11, 8, 10, 0)
    r = route(word, now, "new_booking")
    assert r.intent == "new_booking" and r.slots["date_start"] == "2026-11-09"
    assert route("electrician " + word, now).slots["date_start"] == "2026-11-09"


def _pick_and_compare(tmp_path, advance_s):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1, hold_minutes=1))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    assert a.gate.active() is not None
    clock.advance(seconds=advance_s)
    return a, a.handle_user_message("compare my options")


def test_compare_says_hold_expired_when_hold_lapsed(tmp_path):
    a, out = _pick_and_compare(tmp_path, 120)
    assert "your pick (hold expired; I'll re-hold when you say yes)" in out
    assert "already held" not in out


def test_compare_mentions_live_hold_with_time_left(tmp_path):
    a, out = _pick_and_compare(tmp_path, 10)
    assert "your pick (held, 1 min left)" in out and "expired" not in out


def test_informational_turns_do_not_change_state(tmp_path):
    a, env, conn, clock = mk(tmp_path, Scenario(seed=1))
    a.handle_user_message(f"electrician in Indiranagar on {MON}")
    n = len(events(a, "state_transition"))
    for msg in ("compare my options", "what do the reviews say", "how is Ravi Kumar Electricals rated"):
        a.handle_user_message(msg)
        assert a.store.load().state == S.AWAITING_APPROVAL, msg
    assert len(events(a, "state_transition")) == n
