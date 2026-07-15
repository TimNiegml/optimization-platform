"""Evaluator = the接入 layer.

Wraps whatever actually produces measurements: a Python function today, a
hardware adapter (motor stage + power meter) tomorrow. The contract is a plain
dict in -> dict out, so the generator/orchestrator never know or care whether
`f` is a simulation or a real device.

An optional `store` (SQLiteStore) durably archives every evaluation for
resume / rollback.
"""
from __future__ import annotations

from typing import Callable, Optional


class Evaluator:
    def __init__(self, func: Callable[[dict[str, float]], dict[str, float]],
                 store: Optional[object] = None):
        self.func = func
        self.store = store
        self.history: list[dict] = []       # every evaluation, for archiving

    def evaluate(self, x: dict[str, float], stage: str = "") -> dict[str, float]:
        y = self.func(x)
        self.history.append({"stage": stage, **x, **y})
        if self.store is not None:
            self.store.append(stage, x, y)
        return y
