"""ModelProvider = the pluggable 'given x, produce y' layer.

The evaluator no longer has to be a hard-coded analytic bench. A model is built
from a small spec and returned as a plain `f(x) -> {y}` callable, so ANY source
of a response can drive the platform through the same interface:

  * analytic     — the built-in simulated benches (BENCHES)
  * dataset_idw  — a surrogate built from USER-COLLECTED data points (inverse
                   distance weighting; numpy-only, permissive). This is the hook
                   for "客户采集了很多数据 → 建模 → 接入调优"。
  * (future)     — gp / rbf / neural / physical: register one more provider and
                   it plugs into the evaluator, canvas and AutoTuner unchanged.

Registration mirrors the algorithm registry, so models are as extensible as
algorithms.
"""
from __future__ import annotations

from typing import Callable

from .demo import bench_func

ModelFn = Callable[[dict[str, float]], dict[str, float]]
ModelBuilder = Callable[[dict], ModelFn]

MODEL_PROVIDERS: dict[str, ModelBuilder] = {}


def register_model(kind: str, builder: ModelBuilder) -> None:
    MODEL_PROVIDERS[kind] = builder


def build_model(spec: dict) -> ModelFn:
    kind = (spec or {}).get("kind", "analytic")
    if kind not in MODEL_PROVIDERS:
        raise ValueError(f"unknown model kind: {kind} (registered: {sorted(MODEL_PROVIDERS)})")
    return MODEL_PROVIDERS[kind](spec)


# ---- analytic (built-in simulated benches) ----
def _analytic(spec: dict) -> ModelFn:
    return bench_func(spec.get("bench", "single_peak"))


register_model("analytic", _analytic)


# ---- dataset surrogate: inverse-distance weighting over collected points ----
def _dataset_idw(spec: dict) -> ModelFn:
    """Build a cheap, dependency-light surrogate from measured data.

    spec = {"kind":"dataset_idw",
            "data":[{"x1":..,"x2":..,"y1":..,"y2":..}, ...],
            "variables":["x1","x2",...], "objectives":["y1","y2"], "power":2.0}
    """
    import numpy as np

    data = spec.get("data") or []
    variables = spec["variables"]
    objectives = spec["objectives"]
    power = float(spec.get("power", 2.0))
    if not data:
        raise ValueError("dataset_idw needs non-empty 'data'")
    P = np.asarray([[float(row[v]) for v in variables] for row in data], float)
    Y = {o: np.asarray([float(row[o]) for row in data], float) for o in objectives}

    def f(x: dict[str, float]) -> dict[str, float]:
        q = np.asarray([float(x[v]) for v in variables], float)
        d = np.sqrt(((P - q) ** 2).sum(axis=1))
        if (d < 1e-12).any():                      # exact hit → return that sample
            i = int(np.argmin(d))
            return {o: float(Y[o][i]) for o in objectives}
        w = 1.0 / (d ** power)
        w = w / w.sum()
        return {o: float((w * Y[o]).sum()) for o in objectives}

    return f


register_model("dataset_idw", _dataset_idw)
