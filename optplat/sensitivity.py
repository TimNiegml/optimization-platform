"""灵敏度采集 — 单轴扫描 → 曲线 + 拟合 + 灵敏度矩阵。

回答的是**测量**问题而不是优化问题：给定一个原点，把每个自变量单独动一动（其余轴钉在
原点），记录每个因变量怎么变，然后

  * 出**曲线**（每个 x→y 一条），人可以直接看线性度与线性范围；
  * 出**灵敏度矩阵** S[y][x] = ∂y/∂x（在原点处的斜率），形状与 `DampedSensitivity`
    的 `sensitivity` 入参完全一致 —— 测完可以直接喂给阻尼灵敏度求解；
  * 可以**换几个原点**重复做，比较矩阵随工作点漂移多少（漂移大 = 非线性强 / 该分段建模）。

设计要点：

1. **走统一执行核**。测量经 `StageEngine.evaluate`，因此安全限位、评估预算熔断、
   按通道选择性读取、耗时统计与归档全部照旧 —— 采集不是绕过平台的旁路。
2. **只读需要的通道**。一次采集只读被选中的那些 y。
3. **线性范围是实测出来的**，不是拍脑袋：以原点处的切线为基准，从原点向两侧走，
   直到偏离超过 `linear_tol × y 跨度` 为止。这正是"这段能当线性用"的工程含义。
4. **测完回到原点**。真实台架上采集不应该把机构留在最后一个扫描点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .engine import StageEngine
from .vocs import VOCS


@dataclass
class SensitivitySpec:
    """一次灵敏度采集的声明。用户只需要给：原点、步距、点数。"""

    variables: list[str]                          # 要动的自变量 x
    objectives: list[str] = field(default_factory=list)   # 要测的因变量 y（空=全部）
    step: float = 0.1                             # 统一步距（绝对值）
    steps: dict[str, float] = field(default_factory=dict)  # 每轴步距，覆盖 step
    n_points: int = 5                             # 每轴点数（含原点，建议奇数）
    origins: list[dict] = field(default_factory=list)      # 原点列表（空=当前操作点）
    fit: str = "linear"                           # linear | quadratic
    linear_tol: float = 0.05                      # 线性范围判据：偏离 ≤ tol × y 跨度
    reuse_origin: bool = True                     # 原点只测一次，各轴曲线共用
    settle_at_origin: bool = True                 # 采集结束回到原点

    def axis_step(self, v: str) -> float:
        return float(self.steps.get(v, self.step))


# ---------------------------------------------------------------- 拟合与线性范围
def fit_curve(xs, ys, x0: float, order: int = 1, linear_tol: float = 0.05) -> dict:
    """拟合一条 x→y 曲线，返回原点处斜率、线性度与实测线性范围。

    `order=1` 直线拟合；`order=2` 二次拟合，斜率取**原点处的导数** 2c₂x₀+c₁
    （曲线弯的时候，这比整段直线的斜率更接近真实的局部灵敏度）。

    线性范围：以过原点、斜率为该处切线的直线为基准，从原点向两侧对称外扩，
    直到某一侧偏离超过 `linear_tol × (y 跨度)`。返回绝对坐标区间。
    """
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    n = len(xs)
    out: dict = {"n": n, "order": order}
    if n < 2:
        return {**out, "slope": 0.0, "r2": 0.0, "note": "点数不足，无法拟合"}

    order = 2 if (order == 2 and n >= 3) else 1
    coef = np.polyfit(xs, ys, order)               # 高次在前
    poly = np.poly1d(coef)
    if order == 2:
        slope = float(2.0 * coef[0] * x0 + coef[1])
        # 二次项在半跨度上贡献不到线性项的十亿分之一 = 纯浮点噪声，报 0 而不是 1e-17
        half = 0.5 * float(xs.max() - xs.min())
        curv = float(2.0 * coef[0])
        out["curvature"] = 0.0 if abs(coef[0]) * half ** 2 <= 1e-9 * max(
            abs(slope) * half, 1e-12) else curv
    else:
        slope = float(coef[0])
        out["curvature"] = 0.0
    y0_fit = float(poly(x0))

    # 线性度：始终以直线拟合衡量（二次模式下也报，这才是"线不线性"的意思）
    lin = np.polyfit(xs, ys, 1)
    resid_lin = ys - np.polyval(lin, xs)
    ss_res = float(np.sum(resid_lin ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2))
    span = float(ys.max() - ys.min())
    out.update({
        "slope": slope,                            # ∂y/∂x @ 原点 → 进矩阵
        "intercept": y0_fit - slope * x0,
        "r2": (1.0 - ss_res / ss_tot) if ss_tot > 0 else 1.0,
        "rmse": float(np.sqrt(ss_res / n)),
        "y_span": span,
        "y0": y0_fit,
    })

    # 实测线性范围：以原点切线为基准向两侧外扩
    tol = linear_tol * span
    if span <= 0:
        out["linear_range"] = None
        out["note"] = "该通道在扫描范围内几乎无变化（y 跨度≈0），灵敏度与线性范围无意义"
        return out
    dev = np.abs(ys - (y0_fit + slope * (xs - x0)))
    out["max_dev_frac"] = float(dev.max() / span)
    order_idx = np.argsort(np.abs(xs - x0))        # 由近及远
    lo = hi = x0
    for i in order_idx:
        if dev[i] > tol:
            break
        lo, hi = min(lo, float(xs[i])), max(hi, float(xs[i]))
    out["linear_range"] = [lo, hi]
    out["linear_span"] = hi - lo
    out["linear_full"] = bool(dev.max() <= tol)    # 整段都线性
    return out


# ---------------------------------------------------------------- 采集
def _offsets(spec: SensitivitySpec, v: str) -> list[float]:
    """以原点为中心的对称偏移；总是含 0（原点），点数为偶数时额外补上。"""
    n, st = max(2, int(spec.n_points)), spec.axis_step(v)
    offs = [(i - (n - 1) / 2.0) * st for i in range(n)]
    if not any(abs(o) < 1e-12 for o in offs):
        offs.append(0.0)
    return sorted(offs)


def _sweep_one_origin(engine: StageEngine, vocs: VOCS, spec: SensitivitySpec,
                      origin: dict, objectives: list[str], warnings: list) -> dict:
    """一个原点：逐轴扫描 → 曲线 + 拟合 + 矩阵。"""
    curves: dict[str, dict] = {v: {"xs": [], "y": {o: [] for o in objectives}}
                               for v in spec.variables}
    y_at_origin: Optional[dict] = None
    if spec.reuse_origin:                          # 原点只测一次，所有轴共用
        y_at_origin = engine.evaluate(dict(origin), "sens:origin", objectives)

    for v in spec.variables:
        var = vocs.variables[v]
        for off in _offsets(spec, v):
            x_target = origin[v] + off
            x_clip = var.clip(x_target)
            if abs(x_clip - x_target) > 1e-12:
                warnings.append(f"{v}={x_target:.4g} 超出量程 [{var.low}, {var.high}]，"
                                f"已夹到 {x_clip:.4g}（该点会与相邻点重合，建议减小步距或点数）")
            if abs(off) < 1e-12 and y_at_origin is not None:
                y = y_at_origin                    # 复用原点那次采集
            else:
                y = engine.evaluate({**origin, v: x_clip}, f"sens:{v}", objectives)
            curves[v]["xs"].append(x_clip)
            for o in objectives:
                curves[v]["y"][o].append(float(y.get(o, float("nan"))))

    order = 2 if str(spec.fit).startswith("quad") else 1
    fits: dict[str, dict] = {o: {} for o in objectives}
    matrix: dict[str, dict] = {o: {} for o in objectives}
    for v in spec.variables:
        for o in objectives:
            f = fit_curve(curves[v]["xs"], curves[v]["y"][o], origin[v],
                          order=order, linear_tol=spec.linear_tol)
            fits[o][v] = f
            matrix[o][v] = f["slope"]
    return {"origin": dict(origin), "curves": curves, "fits": fits, "matrix": matrix}


def run_sensitivity(vocs: VOCS, evaluator, spec: SensitivitySpec,
                    start_point: Optional[dict] = None,
                    eval_budget: int = 2000) -> dict:
    """跑一次灵敏度采集（可多原点），返回曲线、拟合、灵敏度矩阵。

    测量全部经 `StageEngine.evaluate` —— 安全限位、预算熔断、按通道读取、耗时统计
    与运行时其它路径完全一致。
    """
    unknown = [v for v in spec.variables if v not in vocs.variables]
    if unknown:
        raise ValueError(f"未知自变量：{unknown}（可用：{list(vocs.variables)}）")
    objectives = [o for o in (spec.objectives or list(vocs.objectives))
                  if o in vocs.objectives]
    if not spec.variables or not objectives:
        raise ValueError("灵敏度采集需要至少一个自变量 x 和一个因变量 y")

    base = dict(start_point or vocs.initial_point())
    origins = [{**base, **o} for o in (spec.origins or [{}])]

    engine = StageEngine(vocs, evaluator, eval_budget=eval_budget)
    engine.state = dict(base)
    warnings: list[str] = []
    per_origin = [_sweep_one_origin(engine, vocs, spec, o, objectives, warnings)
                  for o in origins]

    if spec.settle_at_origin and origins:         # 真实台架：别把机构留在最后一个扫描点
        engine.evaluate(dict(origins[0]), "sens:settle", objectives)

    # 多原点：逐元素统计 —— 均值给"总体"矩阵，标准差看灵敏度随工作点漂移多少
    matrix = {o: {} for o in objectives}
    matrix_std = {o: {} for o in objectives}
    for o in objectives:
        for v in spec.variables:
            vals = [r["matrix"][o][v] for r in per_origin]
            mean = float(np.mean(vals))
            sd = float(np.std(vals)) if len(vals) > 1 else 0.0
            matrix[o][v] = mean
            # 相对量级到浮点噪声级别就是 0 漂移，别显示 ±3e-16
            matrix_std[o][v] = 0.0 if sd <= 1e-9 * max(abs(mean), 1e-12) else sd

    return {
        "variables": list(spec.variables),
        "objectives": objectives,
        "fit": "quadratic" if str(spec.fit).startswith("quad") else "linear",
        "origins": per_origin,
        "matrix": matrix,                          # {y: {x: ∂y/∂x}} → 直接喂阻尼灵敏度
        "matrix_std": matrix_std,                  # 多原点时的漂移；单原点全 0
        "n_origins": len(per_origin),
        "n_evals": engine.n_evals,
        "sim_seconds": getattr(evaluator, "sim_seconds", 0.0),
        "reads": getattr(evaluator, "reads", {}),
        "warnings": warnings,
        "summary": summarize(per_origin, matrix, matrix_std, objectives, spec.variables),
    }


def summarize(per_origin: list, matrix: dict, matrix_std: dict,
              objectives: list[str], variables: list[str]) -> list[str]:
    """给人看的一句话结论：哪对最灵敏、哪对不线性、灵敏度随原点漂移多大。"""
    lines = []
    flat = [(abs(matrix[o][v]), o, v) for o in objectives for v in variables]
    if flat:
        mag, o, v = max(flat)
        lines.append(f"最灵敏：∂{o}/∂{v} = {matrix[o][v]:.4g}")
        weak = [f"{o}/{v}" for m, o, v in flat if mag > 0 and m < 0.01 * mag]
        if weak:
            lines.append(f"几乎无响应（<最强的 1%）：{', '.join(weak)}")
    bad = []
    for r in per_origin:
        for o in objectives:
            for v in variables:
                f = r["fits"][o][v]
                if f.get("linear_range") is not None and not f.get("linear_full", False):
                    lo, hi = f["linear_range"]
                    bad.append(f"{o}←{v} 线性范围 [{lo:.4g}, {hi:.4g}]"
                               f"（R²={f['r2']:.3f}）")
    if bad:
        lines.append("非全段线性：" + "；".join(sorted(set(bad))[:6]))
    if len(per_origin) > 1:
        drift = [(matrix_std[o][v] / abs(matrix[o][v]), o, v)
                 for o in objectives for v in variables if abs(matrix[o][v]) > 1e-12]
        if drift:
            d, o, v = max(drift)
            lines.append(f"随原点漂移最大：∂{o}/∂{v} 相对标准差 {d:.1%}"
                         + ("（>20%，建议分段/按工作点建模）" if d > 0.2 else ""))
    return lines
