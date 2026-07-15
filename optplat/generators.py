"""Generators = the algorithm layer.

Every algorithm speaks the same tiny protocol:

    x  = gen.ask()          # dict of the variables THIS generator controls
    ...evaluate x somewhere...
    gen.tell(x, score)      # feed back a 'higher is better' score
    gen.done                # True when the stage should stop

The generator only optimises its own subset of variables; the orchestrator
holds every other variable fixed during the stage, so the objective looks
like a function of the subset alone.

Keeping this interface identical to Xopt's ask/tell means we can later drop in
Xopt (Apache-2.0) generators — Bayesian, NSGA-II — without touching anything
else. For the 24h MVP we hand-roll two, using only scipy/lmfit (both BSD).
"""
from __future__ import annotations

from typing import Optional

from .vocs import VOCS


class Generator:
    """Base class. Subclasses implement ask/tell and set self.done."""

    def __init__(self, vocs: VOCS, variables: list[str], objective: str):
        self.vocs = vocs
        self.variables = variables
        self.objective = objective
        self.done = False
        self.failed = False          # set True if the algorithm gives up (e.g. bad fit)
        self.best_x: Optional[dict[str, float]] = None
        self.best_score: float = float("-inf")

    def set_base(self, point: dict[str, float]) -> None:
        """Seed the starting operating point (only our subset is read)."""
        self._base = {v: point[v] for v in self.variables}

    def _record(self, x: dict[str, float], score: float) -> None:
        if score > self.best_score:
            self.best_score = score
            self.best_x = dict(x)

    # -- to be overridden --
    def ask(self) -> dict[str, float]:            # pragma: no cover - abstract
        raise NotImplementedError

    def tell(self, x: dict[str, float], score: float) -> None:   # pragma: no cover
        raise NotImplementedError


class CoordinateDescent(Generator):
    """Compass / pattern search over the controlled coordinates.

    One evaluation per ask(): probe +/- step on each axis, move to the best
    improving neighbour, shrink the step when a full sweep finds nothing.
    Robust, derivative-free, and a faithful 'coordinate gradient' for the MVP.
    """

    def __init__(self, vocs, variables, objective, init_step_frac=0.25, tol_frac=1e-3):
        super().__init__(vocs, variables, objective)
        self._step = {
            v: init_step_frac * (vocs.variables[v].high - vocs.variables[v].low)
            for v in variables
        }
        self._tol = {
            v: tol_frac * (vocs.variables[v].high - vocs.variables[v].low)
            for v in variables
        }
        self._base_val: Optional[float] = None
        self._queue: list[dict[str, float]] = []
        self._sweep_best: tuple[float, dict[str, float]] = (float("-inf"), {})
        self._pending: dict[str, float] = {}

    def _neighbours(self) -> list[dict[str, float]]:
        pts = []
        for v in self.variables:
            for sign in (+1, -1):
                cand = dict(self._base)
                cand[v] = self.vocs.variables[v].clip(self._base[v] + sign * self._step[v])
                if cand[v] != self._base[v]:
                    pts.append(cand)
        return pts

    def ask(self) -> dict[str, float]:
        if self._base_val is None:            # evaluate the starting point first
            self._pending = dict(self._base)
            return self._pending
        if not self._queue:                   # begin a fresh sweep
            self._queue = self._neighbours()
            self._sweep_best = (self._base_val, dict(self._base))
            if not self._queue:               # every axis pinned to a bound
                self.done = True
                self._pending = dict(self._base)
                return self._pending
        self._pending = self._queue.pop(0)
        return self._pending

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        if self._base_val is None:
            self._base_val = score
            self._base = {v: x[v] for v in self.variables}
            return
        if score > self._sweep_best[0]:
            self._sweep_best = (score, {v: x[v] for v in self.variables})
        if not self._queue:                   # sweep finished -> decide
            if self._sweep_best[0] > self._base_val:
                self._base_val, self._base = self._sweep_best
            else:                             # no improvement: refine
                for v in self._step:
                    self._step[v] *= 0.5
                if all(self._step[v] < self._tol[v] for v in self._step):
                    self.done = True


class SurrogateFit(Generator):
    """Sample a few points, fit a local model, jump to its extremum.

    Single-variable (the classic 'walk a few points then fit to find the
    peak' case). We always fit in *score* space (higher = better), so the
    optimum is the peak of the fitted model regardless of whether the
    objective is maximize, minimize, or target-value:
      * quadratic  -> vertex of the parabola
      * gaussian   -> centre  (good for optical coupling's ~Gaussian main lobe)

    Guardrails you asked for are built in:
      * fit-quality gate  : reject the fit if R^2 < r2_gate  -> self.failed
      * extrapolation clip: the proposed point is clipped into the range
    """

    def __init__(self, vocs, variables, objective, model="quadratic",
                 n_samples=5, r2_gate=0.9):
        super().__init__(vocs, variables, objective)
        if len(variables) != 1:
            raise ValueError("SurrogateFit (MVP) supports exactly one variable")
        self.var = variables[0]
        self.model = model
        self.n_samples = n_samples
        self.r2_gate = r2_gate
        self._xs: list[float] = []
        self._ys: list[float] = []          # raw objective values
        self._design: list[float] = []      # planned sample abscissae
        self._proposed: Optional[float] = None
        self._pending_val: float = 0.0

    def _build_design(self) -> None:
        v = self.vocs.variables[self.var]
        lo, hi = v.low, v.high
        self._design = [lo + (hi - lo) * i / (self.n_samples - 1)
                        for i in range(self.n_samples)]

    def ask(self) -> dict[str, float]:
        if not self._design and not self._xs:
            self._build_design()
        if self._design:                    # still sampling
            self._pending_val = self._design.pop(0)
        elif self._proposed is not None:    # verify the fitted extremum
            self._pending_val = self._proposed
            self._proposed = None
            self.done = True                # one verify, then stop
        else:                               # fit and propose
            self._fit_and_propose()
            self._pending_val = self._proposed if self._proposed is not None \
                else self.vocs.variables[self.var].clip(self._base[self.var])
        pt = dict(self._base)
        pt[self.var] = self._pending_val
        return pt

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._xs.append(x[self.var])
        # store the raw objective (reconstructed from score via direction);
        # for fitting we use the raw measured y, recovered by the objective.
        self._ys.append(score)

    def _fit_and_propose(self) -> None:
        import numpy as np
        from lmfit.models import GaussianModel, QuadraticModel

        xs = np.asarray(self._xs, float)
        ys = np.asarray(self._ys, float)   # 'higher is better' score space
        v = self.vocs.variables[self.var]
        try:
            if self.model == "gaussian":
                mod = GaussianModel()
                res = mod.fit(ys, mod.guess(ys, x=xs), x=xs)
                x_star = res.params["center"].value
            else:  # quadratic
                mod = QuadraticModel()
                res = mod.fit(ys, mod.guess(ys, x=xs), x=xs)
                a = res.params["a"].value
                b = res.params["b"].value
                if abs(a) < 1e-12:
                    raise ValueError("degenerate quadratic")
                x_star = -b / (2 * a)
            r2 = getattr(res, "rsquared", 1.0)
        except Exception:
            self.failed = True
            self.done = True
            self._proposed = None
            return

        if r2 < self.r2_gate:               # fit-quality gate
            self.failed = True
            self.done = True
            self._proposed = None
            return
        self._proposed = v.clip(x_star)     # extrapolation clip
