"""Seeded randomized invariant sweep (see scripts/check_invariants.py). 100 runs here; the script defaults to 500."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import check_invariants  # noqa: E402


def test_invariant_sweep_100_runs():
    bad = check_invariants.sweep(100)
    assert not bad, "\n".join(f"seed {s}: {v}" for s, v in bad.items())
