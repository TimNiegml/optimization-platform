"""VOCS = Variables, Objectives, Constraints, Statics.

This is the declarative problem-description layer (the "IR"). Everything the
user configures — via form, canvas, or an LLM copilot — ends up as one of
these Pydantic objects. The execution engine only ever reads this, never the UI.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ObjectiveMode(str, Enum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"
    TARGET = "target"      # drive |y - value| -> 0
    SCAN = "scan"          # characterise, no optimum (not exercised by MVP demo)


class ObjectiveValueType(str, Enum):
    SCALAR = "scalar"
    VECTOR = "vector"
    MATRIX = "matrix"


class Variable(BaseModel):
    """A tunable input, e.g. a motor axis position."""
    low: float
    high: float
    resolution: Optional[float] = None   # optional discretisation step

    def clip(self, x: float) -> float:
        return max(self.low, min(self.high, x))

    def mid(self) -> float:
        return 0.5 * (self.low + self.high)


class Objective(BaseModel):
    """A measured output we care about.

    `cost` models the price of *reading this channel once* (e.g. seconds for a
    power-meter integration). The engine only measures the channels a stage
    actually needs, so a step that optimises y1 alone never pays y2's cost.
    `device` / `param` describe which instrument+parameter the channel maps to
    (光功率计 / 指向角 …) — the '通道规范', carried for real-hardware wiring.
    """
    mode: ObjectiveMode = ObjectiveMode.MAXIMIZE
    target: Optional[float] = None       # required when mode == TARGET
    cost: float = 0.0                    # seconds to read this channel once
    device: Optional[str] = None         # e.g. "光功率计"
    param: Optional[str] = None          # e.g. "power" / "指向角"
    group: Optional[str] = None          # measurement group: same group = 并行测量
                                         # (time = max), different groups = 串行 (sum)
    expression: Optional[str] = None     # derived output, e.g. "max(y1,y2)-min(y1,y2)"
    value_type: ObjectiveValueType = ObjectiveValueType.SCALAR

    def direction(self) -> int:
        """+1 if larger score is better, -1 if smaller is better.

        For TARGET we optimise the derived quantity -|y-target|, so bigger is
        always better there; callers should score TARGET via `score()`.
        """
        return -1 if self.mode == ObjectiveMode.MINIMIZE else 1

    def score(self, y: float) -> float:
        """Map a raw measurement to a 'higher is better' score."""
        if self.value_type != ObjectiveValueType.SCALAR:
            raise ValueError(f"{self.value_type.value} objective cannot be scored directly; derive a scalar feature first")
        if self.mode == ObjectiveMode.MINIMIZE:
            return -y
        if self.mode == ObjectiveMode.TARGET:
            assert self.target is not None
            return -abs(y - self.target)
        return y   # MAXIMIZE / SCAN


class VOCS(BaseModel):
    variables: dict[str, Variable] = Field(default_factory=dict)
    objectives: dict[str, Objective] = Field(default_factory=dict)
    # Hard constraints checked when proposing points, expressed as safe
    # expressions over variable + objective names, e.g. "x1 + x2 < 8".
    constraints: list[str] = Field(default_factory=list)

    def initial_point(self) -> dict[str, float]:
        return {name: v.mid() for name, v in self.variables.items()}
