"""Hardware evaluation adapter.

Turns a real (or simulated) instrument set into an Evaluator the orchestrator
can drive, without any layer above knowing it is talking to a device:

    stage : object with  move(axis: str, value: float)       # e.g. motor stage
    meter : object with  read() -> dict[str, float]          # e.g. power meter

Responsibilities that belong here (not in the algorithms):
  * settle time      — wait after moving before reading
  * averaging        — average N noisy reads per point
  * SAFETY LIMITS    — independent hard bounds; by default axes are CLAMPED into
                       the safe range before any move, so no algorithm/orchestrator
                       bug can ever command an out-of-range motion. `strict=True`
                       raises instead of clamping.

Real backend: PyVISA / PyMeasure (both MIT) — write a tiny stage/meter class
whose move()/read() call the instrument. A SimulatedStage/SimulatedMeter pair is
provided so the whole platform runs and is tested without hardware.
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Protocol


class Stage(Protocol):
    def move(self, axis: str, value: float) -> None: ...


class Meter(Protocol):
    def read(self) -> dict[str, float]: ...


class SafetyViolation(RuntimeError):
    pass


class SafetyLimits:
    """Independent physical hard-limits, checked/enforced on every move."""

    def __init__(self, limits: dict[str, tuple[float, float]], strict: bool = False):
        self.limits = limits
        self.strict = strict

    def enforce(self, x: dict[str, float]) -> dict[str, float]:
        out = dict(x)
        for axis, val in x.items():
            if axis not in self.limits:
                continue
            lo, hi = self.limits[axis]
            if val < lo or val > hi:
                if self.strict:
                    raise SafetyViolation(f"{axis}={val} outside safe [{lo}, {hi}]")
                out[axis] = max(lo, min(hi, val))       # clamp to the wall
        return out


class HardwareEvaluator:
    """Evaluator backed by a stage + meter (duck-typed, same API as Evaluator)."""

    def __init__(self, stage: Stage, meter: Meter,
                 settle_time: float = 0.0, averages: int = 1,
                 safety: Optional[SafetyLimits] = None,
                 store: Optional[object] = None,
                 costs: Optional[dict[str, float]] = None):
        self.stage = stage
        self.meter = meter
        self.settle_time = settle_time
        self.averages = max(1, averages)
        self.safety = safety
        self.store = store
        self.costs = costs or {}
        self.history: list[dict] = []
        self.reads: dict[str, int] = {}
        self.sim_seconds: float = 0.0

    def evaluate(self, x: dict[str, float], stage: str = "",
                 channels=None) -> dict[str, float]:
        target = self.safety.enforce(x) if self.safety else x
        for axis, val in target.items():
            self.stage.move(axis, val)
        if self.settle_time:
            time.sleep(self.settle_time)
        reads = [self.meter.read() for _ in range(self.averages)]
        keys = reads[0].keys() if channels is None else [k for k in channels if k in reads[0]]
        y = {k: sum(r[k] for r in reads) / len(reads) for k in keys}
        for k in y:
            self.reads[k] = self.reads.get(k, 0) + 1
            self.sim_seconds += self.costs.get(k, 0.0)
        self.history.append({"stage": stage, **target, **y})
        if self.store is not None:
            self.store.append(stage, target, y)
        return y


# ---- Simulation backend (no hardware needed) --------------------------------
class SimulatedStage:
    def __init__(self, init: dict[str, float]):
        self.pos = dict(init)

    def move(self, axis: str, value: float) -> None:
        self.pos[axis] = value


class SimulatedMeter:
    """Reads the current stage position through a response function, with
    optional additive gaussian noise (so `averages` is exercised)."""

    def __init__(self, stage: SimulatedStage,
                 func: Callable[[dict[str, float]], dict[str, float]],
                 noise: float = 0.0, seed: Optional[int] = None):
        self.stage = stage
        self.func = func
        self.noise = noise
        import random
        self._rng = random.Random(seed)

    def read(self) -> dict[str, float]:
        y = self.func(self.stage.pos)
        if self.noise:
            y = {k: v + self._rng.gauss(0, self.noise) for k, v in y.items()}
        return y
