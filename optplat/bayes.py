"""Bayesian / model-based generators — thin wrappers over permissive OSS.

Kept separate from the hand-rolled core (`generators.py`, numpy/scipy/lmfit
only) so the heavy optional dependency is loaded lazily and the "self-built
core + open-source algorithm wrappers" split stays clean.

Backend: Optuna (MIT) — its native ask()/tell() maps 1:1 onto our Generator
protocol. TPE by default (no extra deps); GP and random are also available.
"""
from __future__ import annotations

import warnings
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
                 sampler: str = "tpe", n_calls: int = 40, seed: Optional[int] = None,
                 span_frac: float = 0.0, n_startup: int = 10, explore: float = 0.1,
                 search_ranges=None):
        super().__init__(vocs, variables, objective)
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        import importlib.util
        self.n_startup = max(1, int(n_startup))
        self.explore = min(0.9, max(0.01, float(explore)))
        # `explore` = TPE 里"好点"分位比例 γ：调大 → 好点集合更宽松 → 模型更保守、
        # 更倾向继续探索；调小 → 只围着最好的几个点挖，收敛快但易陷局部。
        def _gamma(n: int) -> int:
            return max(1, int(round(self.explore * n)))
        if sampler == "gp" and importlib.util.find_spec("torch") is not None:
            try:
                smp = optuna.samplers.GPSampler(seed=seed, n_startup_trials=self.n_startup)
            except TypeError:                        # 老版本没有该参数
                smp = optuna.samplers.GPSampler(seed=seed)
        elif sampler == "random":
            smp = optuna.samplers.RandomSampler(seed=seed)
        else:                                        # tpe, or gp without torch → TPE
            # gamma 在 Optuna 4.9 起标记为弃用（v6 移除）。它是 TPE 里唯一直接的
            # 探索/利用旋钮，所以现在照用（压掉告警噪声），等它真被移除时自动退回
            # 只用 n_startup_trials 控制探索——不会因为升级依赖而崩。
            kw = {"seed": seed, "n_startup_trials": self.n_startup}
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    smp = optuna.samplers.TPESampler(gamma=_gamma, **kw)
            except TypeError:
                smp = optuna.samplers.TPESampler(**kw)
        self._study = optuna.create_study(direction="maximize", sampler=smp)
        self.n_calls = n_calls
        self.span_frac = max(0.0, float(span_frac))
        self.search_ranges = search_ranges or {}
        self._n = 0
        self._trial = None

    def _bounds(self, v: str) -> tuple[float, float]:
        """本次搜索的取值范围。span_frac=0 → 全量程；>0 → 以**起点**为中心、
        该比例的相对窗口（真实台架无绝对坐标，初始搜索通常只在当前位置附近展开）。"""
        var = self.vocs.variables[v]
        explicit = self.search_ranges.get(v)
        if explicit is not None:
            if not isinstance(explicit, (list, tuple)) or len(explicit) != 2:
                raise ValueError(f"search_ranges[{v!r}] must be [low, high]")
            lo, hi = var.clip(float(explicit[0])), var.clip(float(explicit[1]))
            if lo > hi:
                raise ValueError(f"search_ranges[{v!r}] low must be <= high")
            return lo, hi
        if not self.span_frac:
            return var.low, var.high
        half = 0.5 * self.span_frac * (var.high - var.low)
        c = getattr(self, "_base", {}).get(v, var.mid())
        return max(var.low, c - half), min(var.high, c + half)

    def ask(self) -> dict[str, float]:
        self._trial = self._study.ask()
        out = {}
        for v in self.variables:
            lo, hi = self._bounds(v)
            out[v] = self._trial.suggest_float(v, lo, hi) if hi > lo else lo
        return out

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._study.tell(self._trial, float(score))
        self._n += 1
        if self._n >= self.n_calls:
            self.done = True
