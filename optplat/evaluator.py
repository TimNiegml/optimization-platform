"""Evaluator = the接入 layer.

Wraps whatever actually produces measurements: a Python function today, a
hardware adapter (motor stage + power meter) tomorrow. The contract is a plain
dict in -> dict out, so the generator/orchestrator never know or care whether
`f` is a simulation or a real device.

An optional `store` (SQLiteStore) durably archives every evaluation for
resume / rollback.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional


def read_seconds(keys: Iterable[str], costs: dict[str, float],
                 groups: Optional[dict[str, str]] = None) -> float:
    """Modelled time to measure `keys` in ONE acquisition, honouring which
    instruments run concurrently.

    `groups` maps a channel → a group id. Channels in the SAME group are read
    **in parallel** (光功率 + PDL 同表同测), so the group costs `max` of its
    members; different groups are read **serially** (光功率 vs 中心波长 换设备),
    so groups sum. A channel with no group entry is its own serial group.
    `groups` empty/None ⇒ everything serial (plain sum, back-compatible).
    """
    if not groups:
        return sum(costs.get(k, 0.0) for k in keys)
    by_group: dict[str, float] = {}
    for k in keys:
        g = groups.get(k, f"__{k}")            # ungrouped channel = its own group
        by_group[g] = max(by_group.get(g, 0.0), costs.get(k, 0.0))
    return sum(by_group.values())


class Evaluator:
    """Function-backed evaluator with per-channel (selective) reads + cost.

    `evaluate(x, stage, channels)` measures ONLY the requested objective
    channels — a stage that needs y1 alone never reads (or pays for) y2. Each
    channel carries a `cost` (seconds); the evaluator accumulates per-channel
    read counts and total simulated seconds so a workflow's measurement budget
    is visible. `channels=None` reads everything (back-compatible).

    `groups` models instrument concurrency: same-group channels measure in
    parallel (time = max), different groups serially (time sums) — see
    `read_seconds`.
    """

    def __init__(self, func: Callable[[dict[str, float]], dict[str, float]],
                 store: Optional[object] = None,
                 costs: Optional[dict[str, float]] = None,
                 groups: Optional[dict[str, str]] = None):
        self.func = func
        self.store = store
        self.costs = costs or {}
        self.groups = groups or {}
        self.history: list[dict] = []       # every evaluation, for archiving
        self.reads: dict[str, int] = {}     # per-channel read counter
        self.sim_seconds: float = 0.0       # total modelled measurement time

    def evaluate(self, x: dict[str, float], stage: str = "",
                 channels: Optional[Iterable[str]] = None) -> dict[str, float]:
        y_all = self.func(x)
        y = y_all if channels is None else {k: y_all[k] for k in channels if k in y_all}
        for k in y:
            self.reads[k] = self.reads.get(k, 0) + 1
        self.sim_seconds += read_seconds(y.keys(), self.costs, self.groups)
        self.history.append({"stage": stage, **x, **y})
        if self.store is not None:
            self.store.append(stage, x, y)
        return y
