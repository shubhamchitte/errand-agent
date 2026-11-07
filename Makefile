PY := .venv/bin/python
.PHONY: setup test evals demo demo-live sweep trace

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

sweep:
	$(PY) scripts/check_invariants.py 500

trace:
	$(PY) scripts/make_e2e_trace.py
