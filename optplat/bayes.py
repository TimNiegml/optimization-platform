"""Bayesian / model-based generators — thin wrappers over permissive OSS.

Kept separate from the hand-rolled core (`generators.py`, numpy/scipy/lmfit
only) so the heavy optional dependency is loaded lazily and the "self-built
core + open-source algorithm wrappers" split stays clean.

Backend: Optuna (MIT) — its native ask()/tell() maps 1:1 onto our Generator
protocol. TPE by default (no extra deps); GP and random are also available.
"""
from __future__ import annotations

from typing import Optional

from .generators import Generator
from .vocs import VOCS


class BayesianGenerator(Generator):
    """Sequential model-based optimization via Optuna.

    Bayesian optimization does not self-terminate, so it stops after `n_calls`
    evaluations (or earlier via the stage's stop.target / global until). The
    orchestrator moves the operating point to `best_x` at stage end as usual.
    """

    SAMPLERS = ("tpe", "gp", "random")

    def __init__(self, vocs: VOCS, variables: list[str], objective: str,
                 sampler: str = "tpe", n_calls: int = 40, seed: Optional[int] = None):
        super().__init__(vocs, variables, objective)
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        import importlib.util
        if sampler == "gp" and importlib.util.find_spec("torch") is not None:
            smp = optuna.samplers.GPSampler(seed=seed)
        elif sampler == "random":
            smp = optuna.samplers.RandomSampler(seed=seed)
        else:                                        # tpe, or gp without torch → TPE
            smp = optuna.samplers.TPESampler(seed=seed)
        self._study = optuna.create_study(direction="maximize", sampler=smp)
        self.n_calls = n_calls
        self._n = 0
        self._trial = None

    def ask(self) -> dict[str, float]:
        self._trial = self._study.ask()
        return {
            v: self._trial.suggest_float(
                v, self.vocs.variables[v].low, self.vocs.variables[v].high
            )
            for v in self.variables
        }

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._study.tell(self._trial, float(score))
        self._n += 1
        if self._n >= self.n_calls:
            self.done = True
