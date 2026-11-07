"""Inferred guesses are asked about once, plainly, and never used or sent to the model until confirmed."""
import pytest

from evals.harness import MON, World, success_scenario

sc = pytest.mark.scenario
REQ = f"electrician in Indiranagar on {MON} after 6pm"


@sc(category="inferred guess confirmation", expected="relevant unconfirmed guess is asked once; yes -> confirmed and then usable; never sent to the model before; not asked again")
def test_guess_confirmed_on_yes(tmp_path, ev):
    w = World(tmp_path, success_scenario(), ask_guesses=True)
    q = w.say(REQ)
    fact = w.mem.get("inferred.evening")
    assert "Should I remember that?" in q and q.startswith("You seem to be free after 6pm")
    assert not fact.confirmed and w.pending() is None and w.calls("hold_slot") == 0
    assert "Probably free after 6pm" not in w.payload_text()
    w.say("yes")
    ev.actual(f"asked={q!r} confirmed={w.mem.get('inferred.evening').confirmed} pending={w.pending() is not None}")
    assert w.mem.get("inferred.evening").confirmed and w.pending() is not None
    w.say("no")
    w.say(REQ)
    assert len(w.events("guess_question")) == 1


@sc(category="inferred guess confirmation", expected="user says no: the guess is forgotten and never used; the request continues")
def test_guess_forgotten_on_no(tmp_path, ev):
    w = World(tmp_path, success_scenario(), ask_guesses=True)
    w.say(REQ)
    w.say("no")
    ev.actual(f"forgotten={w.mem.get('inferred.evening') is None or w.mem.get('inferred.evening').forgotten}")
    f = w.mem.get("inferred.evening")
    assert f is None or f.forgotten or not f.confirmed
    assert all(f.key != "inferred.evening" for f in w.mem.all())
    assert "Probably free after 6pm" not in w.payload_text()
