"""User-facing wording (calm failures, rupee formatting) and per-turn / per-run INR cost."""
from errand_agent.mocks import Scenario
from errand_agent.models import ModelReply
from errand_agent.text import inr, when
from errand_agent.trace import USD_TO_INR, Tracer, usd_to_inr
from evals.harness import MON, World, success_scenario


def test_inr_and_when():
    assert inr(1450) == "₹1,450" and inr(700) == "₹700" and inr(None) == "n/a"
    assert when("2026-11-09T10:00") == "Mon 9 Nov, 10:00"


def test_timeout_but_booked_reassures_once_not_twice(tmp_path):
    w = World(tmp_path, Scenario(seed=1).force("book", 1, "timeout_booked"))
    w.say(f"electrician in Indiranagar on {MON}")
    out = w.say("yes")
    assert "once, not twice" in out and "Booked (verified)" in out and len(w.new_bookings()) == 1


def test_503_and_hold_expired_messages_are_calm(tmp_path):
    w = World(tmp_path, Scenario(seed=1).force("book", 1, "transient_error").force("book", 2, "transient_error").force("book", 3, "transient_error"))
    w.say(f"electrician in Indiranagar on {MON}")
    out = w.say("yes")
    assert "503" in out and "Nothing was booked or charged" in out
    w2 = World(tmp_path / "b", Scenario(seed=1).force("book", 1, "hold_expired"))
    w2.say(f"electrician in Indiranagar on {MON}")
    assert "nothing was charged" in w2.say("yes").lower()


def test_turn_and_run_cost_in_inr(tmp_path):
    w = World(tmp_path, success_scenario(), script=[ModelReply(text="hi", usage={"model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.01})])
    w.say("hello")
    t = w.events("turn_cost")[-1]
    assert t["cost_usd"] == 0.01 and t["cost_inr"] == usd_to_inr(0.01) == round(0.01 * USD_TO_INR, 4)
    assert w.events("model_call")[-1]["cost_inr"] == t["cost_inr"]
    assert w.tracer.summary()["cost_inr"] == t["cost_inr"]
