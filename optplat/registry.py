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
    GradientAscent,
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
    label: str = ""       # human-facing (Chinese) name; falls back to `name`
    desc: str = ""        # one-line 说明, shown as a canvas tooltip


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
        {"name": s.name, "label": s.label or s.name, "desc": s.desc,
         "category": s.category, "single_var": s.single_var, "params": s.params}
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
        {"n_per_axis": {"type": "int", "default": 7, "min": 3, "max": 21,
                        "label": "每轴取点数"}},
        label="网格扫描", desc="第一阶段找光：在选定变量上打网格粗扫，快速定位大致位置。",
    ),
    AlgorithmSpec(
        "line_scan", "find-light", False,
        lambda v, s: GridScan(v, s["variables"], s["objective"],
                              n_per_axis=s.get("n_per_axis", 9)),
        {"n_per_axis": {"type": "int", "default": 9, "min": 3, "max": 51,
                        "label": "取点数"}},
        label="线扫描", desc="沿单/少数轴逐点扫描，产特性曲线或找过阈值的第一束光。",
    ),
    AlgorithmSpec(
        "coordinate_descent", "local", False,
        lambda v, s: CoordinateDescent(v, s["variables"], s["objective"]),
        label="坐标下降", desc="逐个坐标轴交替精调，稳健的本地精调算法。",
    ),
    AlgorithmSpec(
        "nelder_mead", "local", False,
        lambda v, s: NelderMead(v, s["variables"], s["objective"]),
        label="单纯形法", desc="Nelder-Mead 无导数本地优化，适合多变量峰值精调。",
    ),
    AlgorithmSpec(
        "gradient_ascent", "local", False,
        lambda v, s: GradientAscent(v, s["variables"], s["objective"],
                                    probe_frac=s.get("probe_frac", 0.02),
                                    step_frac=s.get("step_frac", 0.15)),
        {"step_frac": {"type": "float", "default": 0.15, "min": 0.01, "max": 0.5,
                       "label": "步长比例"},
         "probe_frac": {"type": "float", "default": 0.02, "min": 0.005, "max": 0.1,
                        "label": "探测步比例"}},
        label="梯度上升(PI闪电式)",
        desc="有限差分测局部梯度、沿上升方向步进+步长自适应，模拟 PI 闪电式快速对准。",
    ),
    AlgorithmSpec(
        "quadratic_fit", "fit", True,
        lambda v, s: SurrogateFit(v, s["variables"], s["objective"], model="quadratic",
                                  n_samples=s.get("n_samples", 5), r2_gate=s.get("r2_gate", 0.9)),
        {"n_samples": {"type": "int", "default": 5, "min": 3, "max": 15, "label": "采样点数"},
         "r2_gate": {"type": "float", "default": 0.9, "min": 0.0, "max": 0.99,
                     "label": "R² 守门阈值"}},
        label="二次拟合", desc="采几点拟合抛物线定峰；R² 不达标自动回退，防外推跑飞。",
    ),
    AlgorithmSpec(
        "gaussian_fit", "fit", True,
        lambda v, s: SurrogateFit(v, s["variables"], s["objective"], model="gaussian",
                                  n_samples=s.get("n_samples", 5), r2_gate=s.get("r2_gate", 0.9)),
        {"n_samples": {"type": "int", "default": 5, "min": 3, "max": 15, "label": "采样点数"},
         "r2_gate": {"type": "float", "default": 0.9, "min": 0.0, "max": 0.99,
                     "label": "R² 守门阈值"}},
        label="高斯拟合", desc="用高斯峰型拟合定峰，适合耦合功率这类钟形曲线。",
    ),
    AlgorithmSpec(
        "parametric_fit", "fit", True,
        lambda v, s: ParametricFit(v, s["variables"], s["objective"],
                                   model=s.get("model", "quadratic"), fixed=s.get("fixed"),
                                   hints=s.get("hints"), n_samples=s.get("n_samples", 5),
                                   r2_gate=s.get("r2_gate", 0.9)),
        {"model": {"type": "str", "default": "quadratic",
                   "hint": "gaussian / quadratic / 自定义表达式", "label": "模型"},
         "fixed": {"type": "dict", "default": {}, "hint": "钉死的已知参数", "label": "钉死参数"},
         "n_samples": {"type": "int", "default": 5, "min": 3, "max": 15, "label": "采样点数"}},
        label="参数拟合(非标)", desc="非标拟合：钉死已知参数、只解自由参数，支持自定义模型表达式。",
    ),
    AlgorithmSpec(
        "formula", "analytic", True,
        lambda v, s: FormulaMethod(v, s["variables"], s["objective"],
                                   span_frac=s.get("span_frac", 0.5)),
        {"span_frac": {"type": "float", "default": 0.5, "min": 0.1, "max": 1.0,
                       "label": "采样跨度比例"}},
        label="公式法(解析)", desc="三点解析定峰，参数拟合的快速特例，几个点直接算出极值位置。",
    ),
    AlgorithmSpec(
        "bayesian", "bayesian", False, _bayes_builder,
        {"sampler": {"type": "enum", "default": "tpe", "options": ["tpe", "gp", "random"],
                     "label": "采样器"},
         "n_calls": {"type": "int", "default": 40, "min": 10, "max": 200, "label": "评估预算"}},
        label="贝叶斯优化", desc="Optuna 全局优化(TPE/GP)，评估昂贵、多变量、有约束时优先用。",
    ),
]

for _spec in _BUILTINS:
    register_algorithm(_spec)
