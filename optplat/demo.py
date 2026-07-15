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
