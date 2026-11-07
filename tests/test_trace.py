import json

from errand_agent.trace import Tracer, estimate_cost, sanitize


def lines(t):
    return [json.loads(l) for l in t.path.read_text().splitlines()]


def test_jsonl_events(tmp_path):
    t = Tracer("r1", tmp_path)
    t.state_transition("IDLE", "SEARCHING", "intent")
    t.model_call("scripted", 100, 20, 0.001, 12, ["m001"], "abc", 80)
    t.tool_call("search", {"q": "x"}, {"ok": 1}, 5)
    t.tool_call("book", {}, None, 7, error="timeout")
    t.event("custom", a=1)
    ev = lines(t)
    assert len(ev) == 5
    assert all({"ts", "run_id", "type"} <= e.keys() and e["run_id"] == "r1" for e in ev)
    assert ev[1]["context_fact_ids"] == ["m001"] and ev[1]["context_hash"] == "abc" and ev[1]["context_tokens"] == 80
    assert ev[3]["error"] == "timeout"
    assert t.path.parent == tmp_path


def test_summary(tmp_path):
    t = Tracer("r2", tmp_path)
    t.model_call("scripted", 100, 20, 0.5, 10, [], "h", 1)
    t.model_call("scripted", 50, 5, 0.25, 4, [], "h", 1)
    t.tool_call("a", {}, {}, 3)
    t.tool_call("b", {}, {}, 2, error="boom")
    t.state_transition("A", "B", "")
    s = t.summary()
    assert s["input_tokens"] == 150 and s["output_tokens"] == 25 and s["cost_usd"] == 0.75
    assert s["model_calls"] == 2 and s["tool_calls"] == 2 and s["tool_errors"] == 1
    assert s["state_transitions"] == 1 and s["model_latency_ms"] == 14 and s["tool_latency_ms"] == 5


def test_sanitize():
    out = sanitize({"password": "x", "api_key": "k", "gate_code": "1234", "note": "y" * 500,
                    "h": "Authorization Bearer abcdefghijkl", "n": [1, {"token": "t"}], "k": "sk-abcdefghijkl"})
    assert out["password"] == out["api_key"] == out["gate_code"] == "[REDACTED]"
    assert len(out["note"]) < 260 and "chars]" in out["note"]
    assert "abcdefghijkl" not in out["h"] and out["k"] == "[REDACTED]"
    assert out["n"][1]["token"] == "[REDACTED]"


def test_secrets_not_written(tmp_path):
    t = Tracer("r3", tmp_path)
    t.tool_call("login", {"password": "hunter2"}, {"msg": "Bearer abcdefghijkl"}, 1)
    assert "hunter2" not in t.path.read_text() and "abcdefghijkl" not in t.path.read_text()


def test_cost():
    assert estimate_cost("scripted", 1000, 1000) == 0
    assert estimate_cost("claude-sonnet-x", 1_000_000, 1_000_000) == 18.0
    assert estimate_cost("unknown", 1_000_000, 0) == 3.0
