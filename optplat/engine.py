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

import re
from typing import Iterable, Optional

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
from .vocs import VOCS, Objective, ObjectiveMode


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
        self.fits: dict[str, str] = {}            # stage name -> fitted-formula summary
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

    # ---- per-stage objective (node may override the VOCS default) ----
    def _objective(self, step: dict, obj_name: str) -> Objective:
        """Objective for this stage.

        Defaults to the VOCS-declared objective, but a node may override the
        optimisation goal locally — `objective_mode` (maximize / minimize /
        target / scan) and `objective_target` — so the same measured output can
        be driven differently in different stages of one workflow.
        """
        base = self.vocs.objectives[obj_name]
        mode = step.get("objective_mode")
        if not mode:
            return base
        target = step.get("objective_target", base.target)
        return Objective(mode=ObjectiveMode(mode), target=target)

    # ---- which objective channels a stage actually needs to read ----
    def _objs_in(self, expr: Optional[str]) -> set:
        if not expr:
            return set()
        toks = set(re.findall(r"[A-Za-z_]\w*", str(expr)))
        return toks & set(self.vocs.objectives)

    def _needed_channels(self, step: dict) -> set:
        """Union of the stage objective + every objective referenced by its
        keep / stop.target / the global until — so we read exactly what the
        stage needs to optimise AND to evaluate its stopping conditions, and
        skip the rest (and their cost)."""
        need = {step["objective"]}
        need |= set(step.get("objective_weights") or {})       # composite objective refs
        need |= set(step.get("targets") or {})                 # multi-out solver targets
        need |= self._objs_in(step.get("keep"))
        need |= self._objs_in(step.get("stop", {}).get("target"))
        need |= self._objs_in(self.global_until)
        return need & set(self.vocs.objectives)

    # ---- scalar score for a stage (single objective OR weighted composite) ----
    def _scorer(self, step: dict, obj_name: str):
        """Return a callable y_dict -> scalar score (higher = better).

        A node may optimise a single objective (default), or a **weighted
        composite** of several objectives via `objective_weights`
        (e.g. {"y1":0.7,"y2":0.3}) — each objective is scored mode-aware
        (max/min/target) then combined, so a single-objective operator can drive
        a multi-objective trade-off.
        """
        weights = step.get("objective_weights")
        if weights:
            objs = {k: self.vocs.objectives[k] for k in weights if k in self.vocs.objectives}
            return lambda y: sum(w * objs[k].score(y[k]) for k, w in weights.items() if k in objs)
        obj = self._objective(step, obj_name)
        return lambda y: obj.score(y[obj_name])

    # ---- one measurement ----
    def evaluate(self, x: dict[str, float], stage: str,
                 channels: Optional[Iterable[str]] = None) -> dict[str, float]:
        self.n_evals += 1
        if self.n_evals > self.eval_budget:
            self.events.append("⛔ evaluation budget exhausted → stop")
            raise StopAll
        y = self.evaluator.evaluate(x, stage=stage, channels=channels)
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
        scorer = self._scorer(step, obj_name)
        keep = step.get("keep")
        max_iter = step.get("stop", {}).get("max_iter", 300)

        gen = self.make_generator(step)
        gen.set_base(self.state)
        channels = self._needed_channels(step)
        if hasattr(gen, "channels"):                   # solver declares extra reads
            channels |= (set(gen.channels()) & set(self.vocs.objectives))
        goal = (f"加权组合{step['objective_weights']}" if step.get("objective_weights") else obj_name)
        self.events.append(
            f"▶ stage '{name}': {step['algorithm']} on {step['variables']} → {goal}"
            f"  [读取通道 {sorted(channels)}]")

        try:
            for _ in range(max_iter):
                sub_x = gen.ask()
                x = {**self.state, **sub_x}
                y = self.evaluate(x, name, channels)
                if hasattr(gen, "observe"):            # full y vector for multi-out solvers
                    gen.observe(sub_x, y)
                score = scorer(y)
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
                self.evaluate(dict(self.state), f"{name}:settle", channels)
        finally:
            # record the fitted formula even if a global early-stop unwound us
            info = getattr(gen, "fit_info", None)
            if info:
                self.fits[name] = info
                self.events.append(f"   拟合公式：{info}")
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
            "fits": self.fits,
            "reads": getattr(self.evaluator, "reads", {}),
            "sim_seconds": getattr(self.evaluator, "sim_seconds", 0.0),
        }
