"""Orchestrator = the编排 layer, and the real differentiator.

It interprets a declarative pipeline that supports genuine control flow:

  flow:
    - stage: ...      # run an algorithm on a subset of variables
    - if: "<expr>"    # branch
      then: [ ...steps... ]
      else: [ ...steps... ]
    - loop:           # repeat until converged (bounded!)
        body: [ ...steps... ]
        until: "<expr>"
        max_rounds: 8

  until: "<expr>"     # OPTIONAL global early-stop: satisfied at ANY moment -> finish

Safety guardrails baked in:
  * every loop MUST carry max_rounds (no infinite loops -> no runaway motors)
  * conditions are evaluated by asteval on a whitelist of the current
    variable + objective values only (no arbitrary code execution)
  * a global evaluation budget hard-stops everything
"""
from __future__ import annotations

from typing import Any, Optional

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
from .vocs import VOCS


class _StopAll(Exception):
    """Raised internally when the global `until` condition is met."""


class Orchestrator:
    def __init__(self, vocs: VOCS, evaluator: Evaluator, pipeline: dict,
                 eval_budget: int = 5000):
        self.vocs = vocs
        self.evaluator = evaluator
        self.pipeline = pipeline
        self.eval_budget = eval_budget
        self.state: dict[str, float] = vocs.initial_point()
        self.last_y: dict[str, float] = {}
        self.n_evals = 0
        self.events: list[str] = []          # human-readable trace for the UI

    # ---- condition evaluation (whitelisted) ----
    def _cond(self, expr: Optional[str]) -> bool:
        if not expr:
            return False
        aeval = Interpreter(minimal=True)
        aeval.symtable.update(self.state)
        aeval.symtable.update(self.last_y)
        return bool(aeval(expr))

    def _global_stop(self) -> None:
        if self._cond(self.pipeline.get("until")):
            self.events.append("✅ global `until` satisfied → stop")
            raise _StopAll

    # ---- generator factory ----
    def _make_generator(self, step: dict) -> Generator:
        algo = step["algorithm"]
        variables = step["variables"]
        objective = step["objective"]
        if algo in ("grid_scan", "line_scan"):        # phase-1 find-light
            return GridScan(self.vocs, variables, objective,
                            n_per_axis=step.get("n_per_axis", 7))
        if algo == "coordinate_descent":
            return CoordinateDescent(self.vocs, variables, objective)
        if algo == "nelder_mead":
            return NelderMead(self.vocs, variables, objective)
        if algo in ("quadratic_fit", "gaussian_fit"):
            model = "gaussian" if algo == "gaussian_fit" else "quadratic"
            return SurrogateFit(self.vocs, variables, objective, model=model,
                                n_samples=step.get("n_samples", 5),
                                r2_gate=step.get("r2_gate", 0.9))
        if algo in ("formula", "formula_method"):      # analytic 3-point peak
            return FormulaMethod(self.vocs, variables, objective,
                                 span_frac=step.get("span_frac", 0.5))
        if algo in ("parametric_fit", "custom_fit"):   # 公式法 / 非标拟合
            return ParametricFit(self.vocs, variables, objective,
                                 model=step.get("model", "quadratic"),
                                 fixed=step.get("fixed"),
                                 hints=step.get("hints"),
                                 n_samples=step.get("n_samples", 5),
                                 r2_gate=step.get("r2_gate", 0.9))
        raise ValueError(f"unknown algorithm: {algo}")

    # ---- run one measurement ----
    def _evaluate(self, x: dict[str, float], stage: str) -> dict[str, float]:
        self.n_evals += 1
        if self.n_evals > self.eval_budget:
            self.events.append("⛔ evaluation budget exhausted → stop")
            raise _StopAll
        y = self.evaluator.evaluate(x, stage=stage)
        self.last_y.update(y)
        return y

    # ---- run a single stage ----
    def _run_stage(self, step: dict) -> None:
        name = step.get("stage", step["algorithm"])
        obj_name = step["objective"]
        obj = self.vocs.objectives[obj_name]
        keep = step.get("keep")
        max_iter = step.get("stop", {}).get("max_iter", 300)

        gen = self._make_generator(step)
        gen.set_base(self.state)
        self.events.append(f"▶ stage '{name}': {step['algorithm']} on {step['variables']} → {obj_name}")

        for _ in range(max_iter):
            sub_x = gen.ask()
            x = {**self.state, **sub_x}
            y = self._evaluate(x, name)
            score = obj.score(y[obj_name])
            feasible = self._cond_local(keep, x, y) if keep else True
            eff_score = score if feasible else score - 1e9   # penalty method
            gen.tell(sub_x, eff_score)
            self._global_stop()
            stage_target = step.get("stop", {}).get("target")
            if stage_target and self._cond(stage_target):
                self.events.append("   stage `stop.target` met")
                break
            if gen.done:
                break

        # move the operating point to the best feasible point found, then settle
        if gen.best_x is not None and gen.best_score > float("-inf"):
            self.state.update(gen.best_x)
            self._evaluate(dict(self.state), f"{name}:settle")
        self.events.append(f"   best {obj_name}={self.last_y.get(obj_name):.4g} at "
                           + ", ".join(f"{v}={self.state[v]:.3g}" for v in step["variables"]))

        # fit-failed fallback (e.g. R^2 below gate) -> degrade to coordinate descent
        if getattr(gen, "failed", False) and step.get("fallback") == "coordinate_descent":
            self.events.append("   ⚠ fit rejected (R² gate) → fallback coordinate_descent")
            self._run_stage({**step, "algorithm": "coordinate_descent",
                             "fallback": None, "stage": f"{name}:fallback"})

    def _cond_local(self, expr: str, x: dict, y: dict) -> bool:
        aeval = Interpreter(minimal=True)
        aeval.symtable.update(x)
        aeval.symtable.update(y)
        return bool(aeval(expr))

    # ---- run a list of steps (recursively handles control nodes) ----
    def _run_steps(self, steps: list[dict]) -> None:
        for step in steps:
            if "if" in step:
                branch = "then" if self._cond(step["if"]) else "else"
                self.events.append(f"◆ if '{step['if']}' → {branch}")
                self._run_steps(step.get(branch, []))
            elif "loop" in step:
                loop = step["loop"]
                max_rounds = loop["max_rounds"]          # required guardrail
                for r in range(max_rounds):
                    self.events.append(f"↻ loop round {r + 1}/{max_rounds}")
                    self._run_steps(loop["body"])
                    if self._cond(loop.get("until")):
                        self.events.append("   loop `until` satisfied → exit")
                        break
                else:
                    self.events.append(f"   loop hit max_rounds={max_rounds} → exit")
            else:
                self._run_stage(step)

    # ---- public entry point ----
    def run(self) -> dict[str, Any]:
        # prime last_y at the starting point so conditions have values to read
        self._evaluate(dict(self.state), "init")
        try:
            self._run_steps(self.pipeline["flow"])
        except _StopAll:
            pass
        return {
            "state": self.state,
            "objectives": self.last_y,
            "n_evals": self.n_evals,
            "events": self.events,
            "history": self.evaluator.history,
        }
