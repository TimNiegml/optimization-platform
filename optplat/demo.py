"""A simulated optical-coupling test bench, so the platform runs with zero
hardware. Stands in for a real Evaluator.

Physical intuition:
  * y1 = coupling power. Peaks (Gaussian main lobe) when the fibre is centred
    over (x1, x2) = (2.0, -1.0). This is the "先把耦合功率调到最大" objective.
  * y2 = a WDL / polarisation balance metric that depends on x3, and we want to
    drive it to a target while NOT letting the coupling power y1 collapse.
"""
from __future__ import annotations

import math

from .vocs import VOCS, Objective, ObjectiveMode, Variable


def _lobe(x1, x2, cx, cy, amp, w):
    return amp * math.exp(-((x1 - cx) ** 2 + (x2 + cy) ** 2) / (2 * w ** 2))


def optical_bench(x: dict[str, float]) -> dict[str, float]:
    """单峰光纤耦合：一个高斯主瓣，峰在 (x1,x2)=(2,-1)。"""
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    y1 = _lobe(x1, x2, 2.0, 1.0, 1.0, 1.2)                  # in [0, 1]
    # y2: balance metric, best (=1) near x3=0.6, but coupling-dependent
    y2 = y1 * math.exp(-((x3 - 0.6) ** 2) / (2 * 0.25 ** 2))
    return {"y1": round(y1, 6), "y2": round(y2, 6)}


def multi_peak_bench(x: dict[str, float]) -> dict[str, float]:
    """多峰光耦合：主瓣 + 几个旁瓣（局部极大），全局搜索/找光才不会卡在旁瓣。"""
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    y1 = max(_lobe(x1, x2, 2.0, 1.0, 1.00, 0.8),            # 全局主瓣
             _lobe(x1, x2, -1.0, -1.5, 0.75, 0.7),          # 旁瓣
             _lobe(x1, x2, 4.5, 2.0, 0.85, 0.9),            # 旁瓣
             _lobe(x1, x2, 0.0, 2.0, 0.60, 0.6))            # 旁瓣
    y2 = y1 * math.exp(-((x3 - 0.6) ** 2) / (2 * 0.25 ** 2))
    return {"y1": round(y1, 6), "y2": round(y2, 6)}


def skew_bench(x: dict[str, float]) -> dict[str, float]:
    """偏斜/相关峰：主瓣沿一条斜轴拉长且 x1,x2 相关，逐轴下降会慢、单纯形/贝叶斯更好。"""
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    u = (x1 - 2.0) + (x2 + 1.0)          # 长轴方向
    v = (x1 - 2.0) - (x2 + 1.0)          # 短轴方向
    y1 = math.exp(-(u ** 2) / (2 * 2.4 ** 2) - (v ** 2) / (2 * 0.5 ** 2))
    y2 = y1 * math.exp(-((x3 - 0.6) ** 2) / (2 * 0.25 ** 2))
    return {"y1": round(y1, 6), "y2": round(y2, 6)}


def _y2_from(y1, x3):
    return y1 * math.exp(-((x3 - 0.6) ** 2) / (2 * 0.25 ** 2))


def rosenbrock_bench(x: dict[str, float]) -> dict[str, float]:
    """经典 Rosenbrock『香蕉谷』：强相关的狭长弯谷，最优在 (1,1)。
    逐轴下降极慢，考验相关变量的联合优化（协方差）。"""
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    f = (1 - x1) ** 2 + 100 * (x2 - x1 ** 2) ** 2      # min 0 @ (1,1)
    y1 = math.exp(-f / 200.0)
    return {"y1": round(y1, 6), "y2": round(_y2_from(y1, x3), 6)}


def rastrigin_bench(x: dict[str, float]) -> dict[str, float]:
    """经典 Rastrigin：大量规则排布的局部极大，全局在 (0,0)。强多峰。"""
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    f = 20 + (x1 ** 2 - 10 * math.cos(2 * math.pi * x1)) \
           + (x2 ** 2 - 10 * math.cos(2 * math.pi * x2))     # min 0 @ (0,0)
    y1 = math.exp(-f / 18.0)
    return {"y1": round(y1, 6), "y2": round(_y2_from(y1, x3), 6)}


def ackley_bench(x: dict[str, float]) -> dict[str, float]:
    """经典 Ackley：近乎平坦的外围 + 中心一个尖锐全局峰在 (0,0)，多峰、易困外围。"""
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    f = (-20 * math.exp(-0.2 * math.sqrt(0.5 * (x1 ** 2 + x2 ** 2)))
         - math.exp(0.5 * (math.cos(2 * math.pi * x1) + math.cos(2 * math.pi * x2)))
         + math.e + 20)                                       # min 0 @ (0,0)
    y1 = math.exp(-f / 6.0)
    return {"y1": round(y1, 6), "y2": round(_y2_from(y1, x3), 6)}


def linear_sens_bench(x: dict[str, float]) -> dict[str, float]:
    """线性灵敏度台：y1,y2 与 x1,x2 线性耦合，∂y/∂x 已知常数，适合『阻尼灵敏度求解』
    做多进多出定值/标定（如 WDL 打架均衡）。
        y1 =  0.8·x1 + 0.3·x2 + 1.0
        y2 =  0.2·x1 − 0.6·x2 + 0.5      （x3 不参与）
    → 灵敏度矩阵 S(行=y,列=x) = [[0.8, 0.3], [0.2, −0.6]]。"""
    x1, x2 = x["x1"], x["x2"]
    y1 = 0.8 * x1 + 0.3 * x2 + 1.0
    y2 = 0.2 * x1 - 0.6 * x2 + 0.5
    return {"y1": round(y1, 6), "y2": round(y2, 6)}


# Selectable simulated benches (the canvas 场景 dropdown reads this via /benches).
# All share the same variable space (x1,x2,x3 / y1,y2) so one VOCS stays valid.
# `smooth` marks continuous surfaces worth drawing as a response-surface contour.
BENCHES = {
    "single_peak": {"label": "单峰光纤耦合", "func": optical_bench, "smooth": True,
                    "desc": "一个高斯主瓣，经典找光→精调，最好上手。"},
    "multi_peak": {"label": "多峰光耦合", "func": multi_peak_bench, "smooth": True,
                   "desc": "主瓣+多个旁瓣（局部极大），考验找光/全局搜索是否会卡旁瓣。"},
    "skew_peak": {"label": "偏斜相关峰", "func": skew_bench, "smooth": True,
                  "desc": "峰沿斜轴拉长、两轴相关，逐轴下降慢，单纯形/贝叶斯更优。"},
    "rosenbrock": {"label": "Rosenbrock 香蕉谷", "func": rosenbrock_bench, "smooth": True,
                   "desc": "经典相关谷（协方差），最优在(1,1)；逐轴下降慢，考验联合优化。"},
    "rastrigin": {"label": "Rastrigin 多峰", "func": rastrigin_bench, "smooth": True,
                  "desc": "经典强多峰，大量局部极大，全局在(0,0)；找光/贝叶斯的试金石。"},
    "ackley": {"label": "Ackley 多峰", "func": ackley_bench, "smooth": True,
               "desc": "经典多峰，外围近平坦、中心尖峰在(0,0)，容易困在外围。"},
    "linear_sens": {"label": "线性灵敏度台(定值)", "func": linear_sens_bench, "smooth": True,
                    "desc": "y1,y2 与 x1,x2 线性耦合，∂y/∂x 已知；配『阻尼灵敏度求解』把 y 逼到目标值。"},
}


def bench_func(name: str):
    return BENCHES.get(name, BENCHES["single_peak"])["func"]


def demo_vocs() -> VOCS:
    return VOCS(
        variables={
            "x1": Variable(low=-2.0, high=6.0),
            "x2": Variable(low=-5.0, high=3.0),
            "x3": Variable(low=0.0, high=1.5),
        },
        objectives={
            # 通道规范 + 读取成本示例：读一次 y1≈1s（光功率计），y2≈2s（偏振分析仪）。
            # 只优化 y1 的阶段不会去读 y2，省掉 2s/点。
            "y1": Objective(mode=ObjectiveMode.MAXIMIZE, cost=1.0,
                            device="光功率计", param="power"),
            "y2": Objective(mode=ObjectiveMode.MAXIMIZE, cost=2.0,
                            device="偏振分析仪", param="wdl_balance"),
        },
    )


# The canonical two-phase workflow:
#   Phase 1  找光: grid-scan x1,x2 until coupling power crosses a threshold.
#   Phase 2  优化: Nelder-Mead peaks y1, then 公式法 (analytic) tunes x3 for y2
#            while keeping y1 > k. Global early-stop ends it once both are good.
TWO_PHASE_PIPELINE = {
    "until": "y1 >= 0.98 and y2 >= 0.95",
    "flow": [
        {                                   # --- Phase 1: find light ---
            "stage": "find_light",
            "algorithm": "grid_scan",
            "variables": ["x1", "x2"],
            "objective": "y1",
            "n_per_axis": 7,
            "stop": {"target": "y1 > 0.2"},         # stop scanning at first light
        },
        {                                   # --- Phase 2a: peak the coupling ---
            "stage": "peak_y1",
            "algorithm": "nelder_mead",
            "variables": ["x1", "x2"],
            "objective": "y1",
            "stop": {"max_iter": 200},
        },
        {                                   # --- Phase 2b: balance y2 (公式法) ---
            "stage": "balance_y2",
            "algorithm": "formula",
            "variables": ["x3"],
            "objective": "y2",
            "keep": "y1 > 0.8",
            "fallback": "coordinate_descent",
        },
    ],
}


# The SAME two-phase workflow, authored as a node+edge graph (Dify-style).
# A drag-drop canvas would emit exactly this JSON; GraphRunner runs it directly.
# Includes a loop: balance_y2 <-> refine_y1 alternate until both converge.
TWO_PHASE_GRAPH = {
    "until": "y1 >= 0.98 and y2 >= 0.95",
    "nodes": [
        {"id": "start", "type": "start"},
        {"id": "find_light", "type": "algorithm", "data": {
            "algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
            "n_per_axis": 7, "stop": {"target": "y1 > 0.2"}}},
        {"id": "peak_y1", "type": "algorithm", "data": {
            "algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1",
            "stop": {"max_iter": 200}}},
        {"id": "balance_y2", "type": "algorithm", "max_visits": 6, "data": {
            "algorithm": "formula", "variables": ["x3"], "objective": "y2",
            "keep": "y1 > 0.8", "fallback": "coordinate_descent"}},
        {"id": "refine_y1", "type": "algorithm", "max_visits": 6, "data": {
            "algorithm": "coordinate_descent", "variables": ["x1", "x2"],
            "objective": "y1", "stop": {"max_iter": 100}}},
        {"id": "end", "type": "end"},
    ],
    "edges": [
        {"source": "start", "target": "find_light"},
        {"source": "find_light", "target": "peak_y1"},
        {"source": "peak_y1", "target": "balance_y2"},
        {"source": "balance_y2", "target": "refine_y1"},
        # loop back while not yet converged, else fall through to end
        {"source": "refine_y1", "target": "balance_y2",
         "condition": "not (y1 >= 0.95 and y2 >= 0.9)"},
        {"source": "refine_y1", "target": "end"},
    ],
}


# The exact example the user described:
#   先用 x1,x2 坐标梯度优化 y1，再用 x3 拟合优化 y2 并保持 y1>k，
#   两步交替直到都收敛（受限循环，最多 6 轮）。
DEMO_PIPELINE = {
    "until": "y1 >= 0.98 and y2 >= 0.9",          # global early-stop
    "flow": [
        {
            "stage": "align_y1",
            "algorithm": "coordinate_descent",
            "variables": ["x1", "x2"],
            "objective": "y1",
            "stop": {"max_iter": 200},
        },
        {
            "loop": {
                "max_rounds": 6,
                "until": "y1 >= 0.95 and y2 >= 0.9",
                "body": [
                    {
                        "stage": "balance_y2",
                        "algorithm": "quadratic_fit",
                        "variables": ["x3"],
                        "objective": "y2",
                        "keep": "y1 > 0.8",           # hold coupling while tuning x3
                        "fallback": "coordinate_descent",
                    },
                    {
                        "stage": "refine_y1",
                        "algorithm": "coordinate_descent",
                        "variables": ["x1", "x2"],
                        "objective": "y1",
                        "stop": {"max_iter": 100},
                    },
                ],
            }
        },
    ],
}
