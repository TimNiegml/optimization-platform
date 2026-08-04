"""Evaluator backends as plug-ins — the y = f(x) side of the IR.

`registry.py` already makes *algorithms* declarative and pluggable; this does the
same for *where the measurement comes from*, so a graph JSON can say

    "evaluator": {"mode": "zemax", "connection": {...}, "binding": {...}}

and the same pipeline runs against a simulated bench, a real motor stage + power
meter, an OpticStudio design, or several of those at once. Backends are keyed by
name and registered the same way algorithms are, so a customer-specific
instrument backend is a few lines and zero core changes.

Every backend returns an object with `evaluate(x: dict, stage: str) -> dict`.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from .evaluator import Evaluator
from .hardware import HardwareEvaluator, SafetyLimits, SimulatedMeter, SimulatedStage
from .vocs import VOCS


class EvaluatorConfig(BaseModel):
    """Declarative evaluator selection (the evaluator half of the IR)."""
    mode: str = "function"                 # backend name, see BACKENDS
    noise: float = 0.0                     # hardware_sim
    averages: int = 1                      # hardware_sim / hardware
    settle_time: float = 0.0
    safety: bool = False                   # clamp to the VOCS box (independent guard)
    safety_limits: Optional[dict[str, tuple[float, float]]] = None
    strict_safety: bool = False
    connection: Optional[dict] = None      # zemax: ZemaxConnection fields
    binding: Optional[dict] = None         # zemax: ZemaxBinding fields
    children: Optional[dict[str, dict]] = None   # composite: name -> {config, variables}
    prefix_outputs: bool = False           # composite


BackendBuilder = Callable[[VOCS, EvaluatorConfig, Optional[Any]], Any]
BACKENDS: dict[str, BackendBuilder] = {}


def register_backend(name: str, builder: BackendBuilder) -> None:
    BACKENDS[name] = builder


def backend_catalog() -> list[str]:
    return sorted(BACKENDS)


def build_evaluator(vocs: VOCS, cfg: EvaluatorConfig, store: Optional[Any] = None) -> Any:
    if cfg.mode not in BACKENDS:
        raise KeyError(f"unknown evaluator backend '{cfg.mode}'. "
                       f"Available: {backend_catalog()}")
    return BACKENDS[cfg.mode](vocs, cfg, store)


def _safety(vocs: VOCS, cfg: EvaluatorConfig) -> Optional[SafetyLimits]:
    limits = cfg.safety_limits
    if limits is None and cfg.safety:
        limits = {n: (v.low, v.high) for n, v in vocs.variables.items()}
    if not limits:
        return None
    return SafetyLimits({k: tuple(v) for k, v in limits.items()}, strict=cfg.strict_safety)


# ---- built-in backends ------------------------------------------------------
def _build_function(vocs: VOCS, cfg: EvaluatorConfig, store):
    from .demo import optical_bench
    return Evaluator(optical_bench, store=store)


def _build_hardware_sim(vocs: VOCS, cfg: EvaluatorConfig, store):
    from .demo import optical_bench
    stage = SimulatedStage(vocs.initial_point())
    meter = SimulatedMeter(stage, optical_bench, noise=cfg.noise, seed=0)
    return HardwareEvaluator(stage, meter, settle_time=cfg.settle_time,
                             averages=cfg.averages, safety=_safety(vocs, cfg), store=store)


def _build_zemax(vocs: VOCS, cfg: EvaluatorConfig, store):
    from .zemax import ZemaxBinding, ZemaxConnection, ZemaxEvaluator
    if not cfg.connection or not cfg.binding:
        raise ValueError("zemax backend needs both `connection` and `binding`")
    return ZemaxEvaluator(ZemaxBinding(**cfg.binding), ZemaxConnection(**cfg.connection),
                          safety=_safety(vocs, cfg), store=store)


def _build_composite(vocs: VOCS, cfg: EvaluatorConfig, store):
    from .zemax import CompositeEvaluator
    if not cfg.children:
        raise ValueError("composite backend needs `children`")
    children = {}
    for name, spec in cfg.children.items():
        child_cfg = EvaluatorConfig(**spec.get("config", {}))
        owned = list(spec.get("variables", vocs.variables.keys()))
        children[name] = (build_evaluator(vocs, child_cfg, None), owned)
    return CompositeEvaluator(children, prefix_outputs=cfg.prefix_outputs, store=store)


register_backend("function", _build_function)
register_backend("hardware_sim", _build_hardware_sim)
register_backend("zemax", _build_zemax)
register_backend("composite", _build_composite)
