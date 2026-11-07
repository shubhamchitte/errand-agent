import pytest

from errand_agent.clock import FakeClock
from errand_agent.db import connect
from errand_agent.memory import MemoryStore, seed_memories, seed_prior_conversation
from errand_agent.tokens import count_tokens


@pytest.fixture
def store():
    return MemoryStore(connect(), FakeClock(), "u_demo")


@pytest.fixture
def seeded(store):
    seed_memories(store)
    return store


def test_tokens():
    assert count_tokens("") == 0
    assert count_tokens("abcd") == 1
    assert count_tokens("abcde") == 2
    assert count_tokens("x" * 400) == 100


def test_remember_upsert_and_correct(store):
    a = store.remember("k", "v1", tags=["t"])
    assert a.source == "explicit" and a.confirmed
    b = store.remember("k", "v2")
    assert b.id == a.id and b.value == "v2" and len(store.all()) == 1
    c = store.correct("k", "v3")
    assert c.value == "v3"
    with pytest.raises(KeyError):
        store.correct("nope", "x")


def test_inferred_unconfirmed_hidden_until_confirm(store):
    f = store.remember("g", "guess", source="inferred", tags=["new_booking"])
    assert not f.confirmed
    assert store.relevant("new_booking", None, [], 10) == []
    store.confirm(f.id)
    assert [x.key for x in store.relevant("new_booking", None, [], 10)] == ["g"]


def test_inferred_does_not_overwrite_explicit(store):
    store.remember("k", "stated", tags=["x"])
    f = store.remember("k", "guessed", source="inferred")
    assert f.value == "stated" and f.source == "explicit"


def test_forget_by_key_and_id(store):
    a = store.remember("a", "1", tags=["new_booking"])
    store.remember("b", "2", tags=["new_booking"])
    assert store.forget("a") and not store.forget("a")
    assert store.forget("nonexistent") is False
    assert [f.key for f in store.relevant("new_booking", None, [], 10)] == ["b"]
    assert [f.key for f in store.all()] == ["b"]
    assert {f.key for f in store.all(include_forgotten=True)} == {"a", "b"}
    assert store.forget(a.id) is False
    store.forget(store.get("b").id)
    assert store.all() == []


def test_remember_revives_forgotten(store):
    store.remember("a", "1", tags=["new_booking"])
    store.forget("a")
    store.remember("a", "2", tags=["new_booking"])
    assert store.relevant("new_booking", None, [], 5)[0].value == "2"


def test_persistence_in_sqlite(tmp_path):
    p = tmp_path / "m.db"
    s1 = MemoryStore(connect(p), FakeClock(), "u")
    s1.remember("a", "1", tags=["x"])
    s2 = MemoryStore(connect(p), FakeClock(), "u")
    assert s2.get("a").value == "1"
    assert MemoryStore(connect(p), FakeClock(), "other").all() == []


def test_updated_at_uses_clock():
    c = FakeClock()
    s = MemoryStore(connect(), c, "u")
    s.remember("a", "1")
    c.advance(hours=1)
    assert s.correct("a", "2").updated_at == "2026-11-07T10:00:00"


def test_seed_shape(seeded):
    facts = seeded.all(include_forgotten=True)
    assert len(facts) >= 50
    assert any(f.source == "inferred" and not f.confirmed for f in facts)
    assert any(f.source == "inferred" and f.confirmed for f in facts)
    assert any(f.forgotten for f in facts)
    assert any(f.source == "explicit" for f in facts)
    conv = seed_prior_conversation()
    assert len(conv) >= 60 and {t["role"] for t in conv} == {"user", "assistant"}
    assert conv == seed_prior_conversation()


def test_relevant_rebook_electrician(seeded):
    got = seeded.relevant("rebook_same_provider", "electrician", ["provider_id", "slot_time"], 8)
    keys = [f.key for f in got]
    assert "pref.electrician" in keys and "pref.earliest_start" in keys
    assert keys[0] == "pref.electrician" or keys[0] == "pref.earliest_start"
    assert len(got) <= 8


def test_relevant_never_leaks(seeded):
    for intent in ["new_booking", "reschedule", "cancel", "rebook_same_provider", "memory_command", "smalltalk"]:
        for svc in [None, "electrician", "ac_repair", "plumber"]:
            for f in seeded.relevant(intent, svc, ["provider_id", "slot_time", "address", "price"], 100):
                assert not f.forgotten
                assert not (f.source == "inferred" and not f.confirmed)
                assert f.category != "personal"
    keys = {f.key for f in seeded.relevant("rebook_same_provider", "electrician", ["provider_id"], 100)}
    assert "old.electrician" not in keys and "inferred.ac_tech_loyal" not in keys


def test_irrelevant_excluded_and_smalltalk_empty(seeded):
    assert seeded.relevant("smalltalk", None, [], 10) == []
    assert not any(f.key.startswith(("diet", "hobby")) for f in seeded.relevant("new_booking", "electrician", ["address"], 100))


def test_deterministic_and_limit(seeded):
    a = seeded.relevant("new_booking", "ac_repair", ["slot_time"], 5)
    b = seeded.relevant("new_booking", "ac_repair", ["slot_time"], 5)
    assert [f.id for f in a] == [f.id for f in b] and len(a) == 5
    assert seeded.relevant("new_booking", None, [], 0) == []


def test_correction_changes_relevant_value(seeded):
    seeded.correct("pref.earliest_start", "Not before 11am")
    got = {f.key: f for f in seeded.relevant("reschedule", None, ["slot_time"], 20)}
    assert got["pref.earliest_start"].value == "Not before 11am"
