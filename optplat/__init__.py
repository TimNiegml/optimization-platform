"""optplat — a lightweight, closed-source-friendly optimization platform core.

Layers (all permissive-licensed deps only: pydantic/scipy/numpy/lmfit/asteval):
  vocs         — declarative problem description (variables/objectives/constraints)
  evaluator    — how a point gets measured (python fn today, hardware later)
  generators   — the algorithm layer, ask/tell (coordinate descent, surrogate fit)
  orchestrator — control-flow pipeline (sequence / if / loop-until / keep / fallback)
"""
from .evaluator import Evaluator
from .generators import (
    CoordinateDescent,
    FormulaMethod,
    Generator,
    GridScan,
    NelderMead,
    ParametricFit,
    SurrogateFit,
)
from .hardware import (
    HardwareEvaluator,
    SafetyLimits,
    SafetyViolation,
    SimulatedMeter,
    SimulatedStage,
)
from .orchestrator import Orchestrator
from .store import SQLiteStore, rollback_to_best
from .vocs import VOCS, Objective, ObjectiveMode, Variable

__all__ = [
    "VOCS", "Variable", "Objective", "ObjectiveMode",
    "Evaluator", "Orchestrator", "Generator",
    "GridScan", "CoordinateDescent", "NelderMead",
    "SurrogateFit", "FormulaMethod", "ParametricFit",
    # P1b
    "HardwareEvaluator", "SafetyLimits", "SafetyViolation",
    "SimulatedStage", "SimulatedMeter",
    "SQLiteStore", "rollback_to_best",
]

# BayesianGenerator is imported lazily (see orchestrator) to keep optuna optional.
