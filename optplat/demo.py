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


def optical_bench(x: dict[str, float]) -> dict[str, float]:
    x1, x2, x3 = x["x1"], x["x2"], x["x3"]
    # Gaussian coupling lobe centred at (2, -1)
    r2 = (x1 - 2.0) ** 2 + (x2 + 1.0) ** 2
    y1 = math.exp(-r2 / (2 * 1.2 ** 2))                     # in [0, 1]
    # y2: balance metric, best (=1) near x3=0.6, but coupling-dependent
    y2 = y1 * math.exp(-((x3 - 0.6) ** 2) / (2 * 0.25 ** 2))
    return {"y1": round(y1, 6), "y2": round(y2, 6)}


def demo_vocs() -> VOCS:
    return VOCS(
        variables={
            "x1": Variable(low=-2.0, high=6.0),
            "x2": Variable(low=-5.0, high=3.0),
            "x3": Variable(low=0.0, high=1.5),
        },
        objectives={
            "y1": Objective(mode=ObjectiveMode.MAXIMIZE),
            "y2": Objective(mode=ObjectiveMode.MAXIMIZE),
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
