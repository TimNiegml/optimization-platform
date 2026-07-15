"""StageEngine = the shared execution core.

Everything that actually touches the problem — instantiating a generator,
running one algorithm stage against the current operating point, evaluating a
point, checking whitelisted conditions, the global early-stop, the eval-budget
breaker — lives here ONCE. Both front-ends drive it:

  * Orchestrator  — the nested-block pipeline (flow / if / loop)
  * GraphRunner   — the node+edge graph (Dify-style), executed as a state machine

So a workflow authored as blocks or as a graph runs through identical,
already-tested stage logic.
"""
from __future__ import annotations

from typing import Optional

from asteval import Interpreter

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
from .registry import build_generator
from .vocs import VOCS


class StopAll(Exception):
    """Raised when a global early-stop or the eval budget ends the whole run."""


class StageEngine:
    def __init__(self, vocs: VOCS, evaluator: Evaluator, eval_budget: int = 5000,
                 start_point: Optional[dict[str, float]] = None):
        self.vocs = vocs
        self.evaluator = evaluator
        self.eval_budget = eval_budget
        self.state: dict[str, float] = dict(start_point) if start_point else vocs.initial_point()
        self.last_y: dict[str, float] = {}
        self._last_x: dict[str, float] = dict(self.state)
        self.n_evals = 0
        self.events: list[str] = []
        self.global_until: Optional[str] = None   # driver sets this; checked per-eval

    # ---- whitelisted condition evaluation ----
    def cond(self, expr: Optional[str]) -> bool:
        if not expr:
            return False
        aeval = Interpreter(minimal=True)
        aeval.symtable.update(self._last_x)       # most recent measured point
        aeval.symtable.update(self.last_y)
        return bool(aeval(expr))

    def _cond_local(self, expr: str, x: dict, y: dict) -> bool:
        aeval = Interpreter(minimal=True)
        aeval.symtable.update(x)
        aeval.symtable.update(y)
        return bool(aeval(expr))

    # ---- generator factory (delegates to the plug-in registry) ----
    def make_generator(self, step: dict) -> Generator:
        return build_generator(self.vocs, step)

    # ---- one measurement ----
    def evaluate(self, x: dict[str, float], stage: str) -> dict[str, float]:
        self.n_evals += 1
        if self.n_evals > self.eval_budget:
            self.events.append("⛔ evaluation budget exhausted → stop")
            raise StopAll
        y = self.evaluator.evaluate(x, stage=stage)
        self.last_y.update(y)
        self._last_x = dict(x)
        return y

    def check_global(self) -> None:
        if self.cond(self.global_until):
            self.state.update(self._last_x)        # sync state to the winning point
            self.events.append("✅ global `until` satisfied → stop")
            raise StopAll

    def prime(self) -> None:
        """Measure the starting point so conditions have values to read."""
        self.evaluate(dict(self.state), "init")

    # ---- run one algorithm stage against the current operating point ----
    def run_stage(self, step: dict) -> None:
        name = step.get("stage", step["algorithm"])
        obj_name = step["objective"]
        obj = self.vocs.objectives[obj_name]
        keep = step.get("keep")
        max_iter = step.get("stop", {}).get("max_iter", 300)

        gen = self.make_generator(step)
        gen.set_base(self.state)
        self.events.append(
            f"▶ stage '{name}': {step['algorithm']} on {step['variables']} → {obj_name}")

        for _ in range(max_iter):
            sub_x = gen.ask()
            x = {**self.state, **sub_x}
            y = self.evaluate(x, name)
            score = obj.score(y[obj_name])
            feasible = self._cond_local(keep, x, y) if keep else True
            eff_score = score if feasible else score - 1e9    # penalty method
            gen.tell(sub_x, eff_score)
            self.check_global()
            stage_target = step.get("stop", {}).get("target")
            if stage_target and self.cond(stage_target):
                self.events.append("   stage `stop.target` met")
                break
            if gen.done:
                break

        if gen.best_x is not None and gen.best_score > float("-inf"):
            self.state.update(gen.best_x)
            self.evaluate(dict(self.state), f"{name}:settle")
        self.events.append(
            f"   best {obj_name}={self.last_y.get(obj_name):.4g} at "
            + ", ".join(f"{v}={self.state[v]:.3g}" for v in step["variables"]))

        if getattr(gen, "failed", False) and step.get("fallback") == "coordinate_descent":
            self.events.append("   ⚠ fit rejected (R² gate) → fallback coordinate_descent")
            self.run_stage({**step, "algorithm": "coordinate_descent",
                            "fallback": None, "stage": f"{name}:fallback"})

    def result(self) -> dict:
        return {
            "state": self.state,
            "objectives": self.last_y,
            "n_evals": self.n_evals,
            "events": self.events,
            "history": self.evaluator.history,
        }
