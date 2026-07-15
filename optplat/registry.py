"""Algorithm / node registry — the plug-in contract.

Every algorithm node declares a small, machine-readable interface:
  * which params it exposes (type + default + range/options)  -> the canvas
    auto-builds the node's config panel from this
  * whether it is single-variable
  * a builder(vocs, step) -> Generator  (ask/tell)

Built-ins are registered below. A CUSTOM algorithm plugs in with:

    from optplat.registry import register_algorithm, AlgorithmSpec
    class MyGen(Generator): ...            # implement ask()/tell()/done/best_x
    register_algorithm(AlgorithmSpec(
        name="my_algo", category="custom", single_var=False,
        params={"gain": {"type": "float", "default": 1.0}},
        builder=lambda vocs, step: MyGen(vocs, step["variables"], step["objective"],
                                         gain=step.get("gain", 1.0)),
    ))

...and it immediately appears as a draggable node and runs in any graph/pipeline.
The engine never hard-codes algorithm names — it only calls build_generator().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .generators import (
    CoordinateDescent,
    FormulaMethod,
    Generator,
    GridScan,
    NelderMead,
    ParametricFit,
    SurrogateFit,
)
from .vocs import VOCS


@dataclass
class AlgorithmSpec:
    name: str
    category: str
    single_var: bool
    builder: Callable[[VOCS, dict], Generator]
    params: dict = field(default_factory=dict)   # param -> {type, default, ...}


REGISTRY: dict[str, AlgorithmSpec] = {}


def register_algorithm(spec: AlgorithmSpec) -> None:
    REGISTRY[spec.name] = spec


def build_generator(vocs: VOCS, step: dict) -> Generator:
    algo = step["algorithm"]
    if algo not in REGISTRY:
        raise ValueError(f"unknown algorithm: {algo} (registered: {sorted(REGISTRY)})")
    return REGISTRY[algo].builder(vocs, step)


def algorithm_catalog() -> list[dict]:
    """Everything the canvas needs to render the node palette + config panels."""
    return [
        {"name": s.name, "category": s.category,
         "single_var": s.single_var, "params": s.params}
        for s in REGISTRY.values()
    ]


# ---------------- built-in algorithm nodes ----------------
def _bayes_builder(vocs, step):
    from .bayes import BayesianGenerator
    return BayesianGenerator(vocs, step["variables"], step["objective"],
                             sampler=step.get("sampler", "tpe"),
                             n_calls=step.get("n_calls", 40), seed=step.get("seed"))


_BUILTINS = [
    AlgorithmSpec(
        "grid_scan", "find-light", False,
        lambda v, s: GridScan(v, s["variables"], s["objective"],
                              n_per_axis=s.get("n_per_axis", 7)),
        {"n_per_axis": {"type": "int", "default": 7, "min": 3, "max": 21}},
    ),
    AlgorithmSpec(
        "line_scan", "find-light", False,
        lambda v, s: GridScan(v, s["variables"], s["objective"],
                              n_per_axis=s.get("n_per_axis", 9)),
        {"n_per_axis": {"type": "int", "default": 9, "min": 3, "max": 51}},
    ),
    AlgorithmSpec(
        "coordinate_descent", "local", False,
        lambda v, s: CoordinateDescent(v, s["variables"], s["objective"]),
    ),
    AlgorithmSpec(
        "nelder_mead", "local", False,
        lambda v, s: NelderMead(v, s["variables"], s["objective"]),
    ),
    AlgorithmSpec(
        "quadratic_fit", "fit", True,
        lambda v, s: SurrogateFit(v, s["variables"], s["objective"], model="quadratic",
                                  n_samples=s.get("n_samples", 5), r2_gate=s.get("r2_gate", 0.9)),
        {"n_samples": {"type": "int", "default": 5, "min": 3, "max": 15},
         "r2_gate": {"type": "float", "default": 0.9, "min": 0.0, "max": 0.99}},
    ),
    AlgorithmSpec(
        "gaussian_fit", "fit", True,
        lambda v, s: SurrogateFit(v, s["variables"], s["objective"], model="gaussian",
                                  n_samples=s.get("n_samples", 5), r2_gate=s.get("r2_gate", 0.9)),
        {"n_samples": {"type": "int", "default": 5, "min": 3, "max": 15},
         "r2_gate": {"type": "float", "default": 0.9, "min": 0.0, "max": 0.99}},
    ),
    AlgorithmSpec(
        "parametric_fit", "fit", True,
        lambda v, s: ParametricFit(v, s["variables"], s["objective"],
                                   model=s.get("model", "quadratic"), fixed=s.get("fixed"),
                                   hints=s.get("hints"), n_samples=s.get("n_samples", 5),
                                   r2_gate=s.get("r2_gate", 0.9)),
        {"model": {"type": "str", "default": "quadratic",
                   "hint": "gaussian / quadratic / 自定义表达式"},
         "fixed": {"type": "dict", "default": {}, "hint": "钉死的已知参数"},
         "n_samples": {"type": "int", "default": 5, "min": 3, "max": 15}},
    ),
    AlgorithmSpec(
        "formula", "analytic", True,
        lambda v, s: FormulaMethod(v, s["variables"], s["objective"],
                                   span_frac=s.get("span_frac", 0.5)),
        {"span_frac": {"type": "float", "default": 0.5, "min": 0.1, "max": 1.0}},
    ),
    AlgorithmSpec(
        "bayesian", "bayesian", False, _bayes_builder,
        {"sampler": {"type": "enum", "default": "tpe", "options": ["tpe", "gp", "random"]},
         "n_calls": {"type": "int", "default": 40, "min": 10, "max": 200}},
    ),
]

for _spec in _BUILTINS:
    register_algorithm(_spec)
