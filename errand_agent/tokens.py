"""Deterministic token estimator (no tokenizer dependency).

Rule: ceil(len(text) / 4), the common ~4 chars/token heuristic; empty text is 0.
Used for the context cap and for trace logging, so identical text always counts identically.
"""
from __future__ import annotations

import math


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / 4)
