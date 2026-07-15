"""Orchestrator = the nested-block pipeline driver.

Interprets the declarative block pipeline (flow / if / loop) by driving the
shared StageEngine. The graph driver (GraphRunner) drives the SAME engine, so
both authoring styles share one tested execution core.

  flow:
    - stage: ...      # run an algorithm stage
    - if: "<expr>"    # branch
      then: [ ... ]
      else: [ ... ]
    - loop:           # bounded repeat
        body: [ ... ]
        until: "<expr>"
        max_rounds: 8
  until: "<expr>"     # OPTIONAL global early-stop

Guardrails (in the engine): every loop MUST carry max_rounds; conditions are
asteval-whitelisted; a global evaluation budget hard-stops everything.
"""
from __future__ import annotations

from typing import Any, Optional

from .engine import StageEngine, StopAll
from .evaluator import Evaluator
from .vocs import VOCS


class Orchestrator:
    def __init__(self, vocs: VOCS, evaluator: Evaluator, pipeline: dict,
                 eval_budget: int = 5000,
                 start_point: Optional[dict[str, float]] = None):
        self.pipeline = pipeline
        self.engine = StageEngine(vocs, evaluator, eval_budget, start_point)
        self.engine.global_until = pipeline.get("until")

    @property
    def state(self) -> dict[str, float]:
        return self.engine.state

    def _run_steps(self, steps: list[dict]) -> None:
        eng = self.engine
        for step in steps:
            if "if" in step:
                branch = "then" if eng.cond(step["if"]) else "else"
                eng.events.append(f"◆ if '{step['if']}' → {branch}")
                self._run_steps(step.get(branch, []))
            elif "loop" in step:
                loop = step["loop"]
                max_rounds = loop["max_rounds"]              # required guardrail
                for r in range(max_rounds):
                    eng.events.append(f"↻ loop round {r + 1}/{max_rounds}")
                    self._run_steps(loop["body"])
                    if eng.cond(loop.get("until")):
                        eng.events.append("   loop `until` satisfied → exit")
                        break
                else:
                    eng.events.append(f"   loop hit max_rounds={max_rounds} → exit")
            else:
                eng.run_stage(step)

    def run(self) -> dict[str, Any]:
        self.engine.prime()
        try:
            self._run_steps(self.pipeline["flow"])
        except StopAll:
            pass
        return self.engine.result()
