PY := .venv/bin/python
.PHONY: setup test evals demo demo-live record record-live record-restart record-restart-live sweep trace

setup:
	python3 -m venv .venv
	$(PY) -m pip install -e .

test:
	$(PY) -m pytest -q tests

evals:
	$(PY) -m pytest -q evals

demo:
	$(PY) -m errand_agent --scripted --reset --scenario timeout_booked

demo-live:
	$(PY) -m errand_agent --reset

# Screen-recording runs (see DEMO.md). Restart after /crash-mid-booking with record-restart (no --reset).
record:
	$(PY) -m errand_agent --scripted --reset --scenario demo

record-live:
	$(PY) -m errand_agent --reset --scenario demo

record-restart:
	$(PY) -m errand_agent --scripted --scenario demo

record-restart-live:
	$(PY) -m errand_agent --scenario demo

sweep:
	$(PY) scripts/check_invariants.py 500

trace:
	$(PY) scripts/make_e2e_trace.py
