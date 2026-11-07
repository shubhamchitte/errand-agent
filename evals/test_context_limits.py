"""Memory correction/forgetting, context budget, limit exhaustion, headline prompts."""
import pytest

from errand_agent import state as S
from errand_agent.agent import Limits
from errand_agent.context import TOKEN_CAP
from errand_agent.mocks import Scenario
from errand_agent.tokens import count_tokens
from evals.harness import MON, SAT, World, call, reply, success_scenario

sc = pytest.mark.scenario

FORGOTTEN_TEXT = ["p_elc_9", "Lived in Koramangala", "Anytime after 8am is fine"]
UNCONFIRMED_TEXT = ["Probably pays by UPI", "Probably wants the same AC tech again", "Probably wants the cheapest option",
                    "Probably free after 6pm"]


# ------------------------------------------------------------------ memory
@sc(category="memory correction", expected="corrected earliest-start is used for the slot and sent to the model; the superseded value never is")
def test_memory_correction(tmp_path, ev):
    """A user correction replaces the stored fact instead of adding a conflicting one."""
    w = World(tmp_path, success_scenario())
    w.mem.correct("pref.earliest_start", "Not before 11am on any day; never schedule visits earlier than 11:00")
    w.say(f"electrician in Indiranagar on {MON}")
    card = w.pending().card
    text = w.payload_text()
    ev.actual(f"earliest_time slot={w.slots()['earliest_time']} card slot={card.slot[11:]} new text sent={'before 11' in text.lower() or 'Not before 11am' in text} old sent={'Not before 10am' in text}")
    assert w.slots()["earliest_time"] == "11:00" and card.slot[11:16] >= "11:00"
    assert "Not before 11am" in text and "Not before 10am" not in text


@sc(category="memory correction", expected="in-chat correction of a slot changes only that field, invalidates the old card and does not re-ask")
def test_in_chat_correction_changes_one_field(tmp_path, ev):
    """A mid-chat correction changes only the named field and keeps the rest."""
    w = World(tmp_path)
    w.say(f"I need a plumber in Indiranagar on {MON} under 1500")
    before, first = w.slots(), w.pending()
    w.say("actually make it Koramangala")
    after = w.slots()
    diff = {k for k in after if after[k] != before[k]}
    ev.actual(f"changed fields={sorted(diff)}; old card status={w.agent.gate.get(first.action_id).status if first else None}; missing={w.model.seen[-1].state['missing']}")
    assert diff == {"area"} and after["area"] == "Koramangala"
    assert w.model.seen[-1].state["missing"] == []
    assert first is None or w.agent.gate.get(first.action_id).status in ("INVALIDATED", "SUPERSEDED")
    assert w.calls("book") == 0


@sc(category="memory forgetting", expected="after 'forget my dog' the fact is flagged forgotten and its text is absent from every later model payload; it was present before")
def test_memory_forgetting(tmp_path, ev):
    """Forgotten facts are no longer used or shown to the model."""
    w = World(tmp_path)
    w.say(f"electrician in Indiranagar on {MON}")
    n_before = len(w.payloads())
    assert "Bruno" in w.payload_text()
    w.say("no")
    out = w.say("forget my dog")
    assert w.mem.get("household.pets").forgotten
    w.say(f"electrician in Indiranagar on {MON}")
    recs = w.payloads()[n_before:]
    later = "\n".join(r["system"] + r["rendered"] for r in recs)
    fid = w.mem.get("household.pets").id
    ev.actual(f"forgotten flag=True; later payloads={len(recs)}; fact id in later fact_ids={any(fid in r['fact_ids'] for r in recs)}; "
              f"fact text in later payloads={'Has a dog' in later}")
    assert recs and not any(fid in r["fact_ids"] for r in recs)
    assert "Has a dog" not in later and "Bruno" not in later


@sc(category="memory forgetting", expected="forgotten facts stay out of payloads and out of slot defaults across a process restart")
def test_forgotten_survives_restart(tmp_path, ev):
    """A forgotten fact stays forgotten after a process restart."""
    w = World(tmp_path, persist=True)
    w.say("forget old.earliest_start")                      # already forgotten at seed -> no-op, must not revive
    w.mem.forget("pref.earliest_start")
    w.restart()
    w.say(f"electrician in Indiranagar on {MON}")
    ev.actual(f"earliest_time slot={w.slots().get('earliest_time')}; forgotten pref sent={'Not before 10am' in w.payload_text()}")
    assert w.slots().get("earliest_time") is None
    assert "Not before 10am" not in w.payload_text() and "Anytime after 8am" not in w.payload_text()


# ------------------------------------------------------------------ context budget
@sc(category="context budget", expected="50+ memories and a 200+ turn history: every model payload is <= the token cap, no forgotten/unconfirmed-inferred text in any payload, relevant prefs present")
def test_context_budget_and_filtering(tmp_path, ev):
    """Model context stays under budget and is filtered to what the request needs."""
    w = World(tmp_path)
    w.seed_history(3)
    n_mem = len(w.mem.all(include_forgotten=True))
    assert n_mem >= 50 and len(w.agent.store.turns(400)) >= 200
    w.say(f"book the same electrician as last time on {MON}")
    w.say("no")
    w.say("cancel my AC servicing booking")
    recs = w.payloads()
    worst = max(r["tokens"] for r in recs)
    ev.actual(f"memories={n_mem}; payloads={len(recs)}; max tokens={worst} (cap {TOKEN_CAP}); leaked forgotten={any(t in r['system'] + r['rendered'] for r in recs for t in FORGOTTEN_TEXT)}")
    assert recs and all(r["tokens"] <= TOKEN_CAP for r in recs)
    assert all(count_tokens(r["system"]) + count_tokens(r["rendered"]) <= TOKEN_CAP for r in recs)
    blob = w.payload_text()
    for t in FORGOTTEN_TEXT + UNCONFIRMED_TEXT:
        assert t not in blob, t
    assert "Not before 10am" in blob and "Ravi" in blob            # relevant current prefs got through
    assert "Vegetarian" not in blob and "acoustic guitar" not in blob
    assert len(recs) == len(w.events("model_call"))
    assert [r["hash"] for r in recs] == [e["context_hash"] for e in w.events("model_call")]


@sc(category="context budget", expected="irrelevant personal memories (diet, hobbies, travel...) never reach the model for a booking; fact_ids logged per call match the sidecar")
def test_context_only_relevant_memories(tmp_path, ev):
    """Only memories relevant to the request are put in the context."""
    w = World(tmp_path)
    w.say(f"plumber in Indiranagar on {MON}")
    recs = w.payloads()
    sent_ids = {i for r in recs for i in r["fact_ids"]}
    sent_keys = {f.key for f in w.mem.all() if f.id in sent_ids}
    irrelevant = {f.key for f in w.mem.all() if f.category == "personal"}
    ev.actual(f"facts sent={len(sent_ids)} of {len(w.mem.all())}; irrelevant sent={sorted(sent_keys & irrelevant)}")
    assert sent_ids and not (sent_keys & irrelevant)
    assert [r["fact_ids"] for r in recs] == [e["context_fact_ids"] for e in w.events("model_call")]
    assert all(r["tokens"] <= TOKEN_CAP for r in recs)


@sc(category="context budget", expected="a 40k-char user message plus huge tool results still yields payloads <= cap (hard cap held)")
def test_context_cap_under_huge_inputs(tmp_path, ev):
    """Huge tool outputs are truncated so context size stays bounded."""
    script = [reply(call("get_provider", provider_id="p_plm_2")), reply(call("list_bookings", user_id="u_demo")),
              reply(call("get_provider", provider_id="p_elc_2")), reply(text="ok")]
    w = World(tmp_path, script=script)
    w.seed_history(3)
    w.say("tell me about providers " + "lorem ipsum dolor " * 2500)
    recs = w.payloads()
    ev.actual(f"payloads={len(recs)} max tokens={max(r['tokens'] for r in recs)} cap={TOKEN_CAP}")
    assert len(recs) == 4 and all(r["tokens"] <= TOKEN_CAP for r in recs)
    assert w.calls("book") == 0


# ------------------------------------------------------------------ limits
def _spin():
    return lambda ctx, tools: reply(call("list_bookings", user_id="u_demo"))


@sc(category="limit exhaustion", expected="endless model tool-calling stops at max_model_turns; limit_exit logged; nothing booked or changed")
def test_limit_model_turns(tmp_path, ev):
    """The loop stops cleanly at the model-turn cap."""
    w = World(tmp_path, script=[_spin()] * 50, limits=Limits(max_model_turns=3, max_tool_calls=99))
    before = w.snapshot()
    w.say("hello")
    ev.actual(f"model calls={len(w.model.seen)} limit_exit={[e['reason'] for e in w.events('limit_exit')]}")
    assert len(w.model.seen) == 3 and [e["reason"] for e in w.events("limit_exit")] == ["max_model_turns"]
    assert w.snapshot() == before and w.pending() is None


@sc(category="limit exhaustion", expected="too many tool calls in one turn stops at max_tool_calls; the (N+1)th call is never executed")
def test_limit_tool_calls(tmp_path, ev):
    """The loop stops cleanly at the tool-call cap."""
    many = reply(*[call("list_bookings", user_id="u_demo") for _ in range(6)])
    w = World(tmp_path, script=[many] * 5, limits=Limits(max_model_turns=99, max_tool_calls=4))
    w.say("hello")
    ev.actual(f"list_bookings executed={w.calls('list_bookings')} limit_exit={[e['reason'] for e in w.events('limit_exit')]}")
    assert w.calls("list_bookings") == 4 and [e["reason"] for e in w.events("limit_exit")] == ["max_tool_calls"]
    assert w.new_bookings() == [] and w.pending() is None


@sc(category="limit exhaustion", expected="wall-clock budget exhausted: loop exits before another model call, nothing booked")
def test_limit_wall_clock(tmp_path, ev):
    """The loop stops cleanly when the wall-clock budget is exceeded."""
    ticks = iter(range(0, 100000, 50))
    w = World(tmp_path, script=[_spin()] * 50, limits=Limits(wall_clock_s=120), timer=lambda: next(ticks))
    w.say("hello")
    ev.actual(f"model calls={len(w.model.seen)} limit_exit={[e['reason'] for e in w.events('limit_exit')]}")
    assert [e["reason"] for e in w.events("limit_exit")] == ["wall_clock"] and 1 <= len(w.model.seen) <= 4
    assert w.new_bookings() == [] and w.pending() is None


@sc(category="limit exhaustion", expected="wall clock expires while the executor is retrying a failing book: stops, never claims success, row not DONE")
def test_limit_wall_clock_during_execution(tmp_path, ev):
    """A timeout during execution leaves state consistent and is reported honestly."""
    sc_ = Scenario(seed=1)
    for n in range(1, 10):
        sc_.force("book", n, "transient_error")
    t = {"v": 0.0}
    w = World(tmp_path, sc_, limits=Limits(wall_clock_s=2.0), timer=lambda: t["v"])
    w.agent.sleep = lambda s: t.__setitem__("v", t["v"] + 5)   # each backoff burns the budget
    w.say(f"electrician in Indiranagar on {MON}")
    out = w.say("yes")
    ev.actual(f"book calls={w.calls('book')} bookings={len(w.new_bookings())} exec={[r['status'] for r in w.exec_rows()]}")
    assert w.new_bookings() == [] and "Booked" not in out
    assert w.calls("book") <= 3 and all(r["status"] != "DONE" for r in w.exec_rows())


# ------------------------------------------------------------------ headline prompts
@sc(category="headline prompts", expected="'plumber, Monday morning, under Rs 1500': proposal is a plumber, morning, <=1500, >=10:00 (memory), in Indiranagar; nothing booked before yes; one booking after")
def test_headline_plumber_mornings_under_1500(tmp_path, ev):
    """Plumber, mornings, under 1500: results respect the budget and time window."""
    w = World(tmp_path, success_scenario())
    w.say(f"Find me a plumber in Indiranagar on {MON} morning under ₹1500")
    c = w.pending().card
    ev.actual(f"card {c.provider_id}@{c.slot} ₹{c.price_inr}; book before yes={w.calls('book')}; search args={w.tool_args('search_providers')[0]}")
    assert c.service == "plumber" and c.price_inr <= 1500 and "10:00" <= c.slot[11:16] < "12:00"
    assert w.slots()["budget_inr"] == 1500 and w.calls("book") == 0
    assert w.tool_args("search_providers")[0]["service"] == "plumber"
    w.say("yes")
    b = w.new_bookings()
    assert len(b) == 1 and b[0]["price"] <= 1500 and b[0]["slot"] == c.slot


@sc(category="headline prompts", expected="'move my AC servicing to Saturday': reschedule card for bk_seed_4 on the Saturday, >=10:00; moves only after yes; no new booking")
def test_headline_move_ac_to_saturday(tmp_path, ev):
    """Moving the AC booking to Saturday needs approval and ends in one moved booking."""
    w = World(tmp_path)
    w.say("Move my AC servicing to Saturday")
    c = w.pending().card
    ev.actual(f"card {c.type} {c.booking_id} -> {c.slot}; reschedule calls before yes={w.calls('reschedule')}")
    assert c.type == "reschedule" and c.booking_id == "bk_seed_4" and c.slot.startswith(SAT) and c.slot[11:16] >= "10:00"
    assert w.calls("reschedule") == 0 and w.booking("bk_seed_4")["slot"] != c.slot
    w.say("yes")
    assert w.booking("bk_seed_4")["slot"] == c.slot and w.calls("reschedule") == 1 and w.new_bookings() == []


@sc(category="headline prompts", expected="'same electrician as last time, not before 10am': proposes p_elc_1 (past booking provider) at >=10:00; booked once after yes")
def test_headline_same_electrician_not_before_10(tmp_path, ev):
    """Rebooking the same electrician honours the not-before-10:00 constraint."""
    w = World(tmp_path, success_scenario())
    w.say("book the same electrician as last time, not before 10am")
    c = w.pending().card
    ev.actual(f"card {c.provider_id}@{c.slot}; earliest_time={w.slots()['earliest_time']}; book before yes={w.calls('book')}")
    assert c.provider_id == "p_elc_1" and c.slot[11:16] >= "10:00" and w.calls("book") == 0
    w.say("yes")
    assert len(w.new_bookings()) == 1 and w.new_bookings()[0]["provider_id"] == "p_elc_1"
    assert w.state() == S.DONE
