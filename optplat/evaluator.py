"""Evaluator = the接入 layer.

Wraps whatever actually produces measurements: a Python function today, a
hardware adapter (motor stage + power meter) tomorrow. The contract is a plain
dict in -> dict out, so the generator/orchestrator never know or care whether
`f` is a simulation or a real device.
"""
from __future__ import annotations

from typing import Callable


class Evaluator:
    def __init__(self, func: Callable[[dict[str, float]], dict[str, float]]):
        self.func = func
        self.history: list[dict] = []       # every evaluation, for archiving

    def evaluate(self, x: dict[str, float], stage: str = "") -> dict[str, float]:
        y = self.func(x)
        self.history.append({"stage": stage, **x, **y})
        return y
