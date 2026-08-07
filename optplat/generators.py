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
else. All hand-rolled generators use only numpy/scipy/lmfit (all BSD).

Algorithm library:
  first-light / scan : GridScan        (grid search; 1 variable = line search)
  local optimisation : CoordinateDescent, NelderMead
  surrogate / fit    : SurrogateFit    (quadratic-fit, gaussian-fit)
  analytic           : FormulaMethod    (3-point parabolic peak, no least-squares)
"""
from __future__ import annotations

import math
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
        self.fit_info: Optional[str] = None   # fitters set a human-readable formula
        self.best_x: Optional[dict[str, float]] = None
        self.best_score: float = float("-inf")

    # Optional richer feedback: if a generator defines observe(), the engine also
    # hands it the FULL measured objective dict (not just the scalar score) after
    # every evaluation. Multi-output solvers (e.g. 阻尼灵敏度求解) need the whole y
    # vector; scalar-score algorithms simply don't implement this.
    # def observe(self, x: dict[str, float], y: dict[str, float]) -> None: ...

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

    def __init__(self, vocs, variables, objective, init_step_frac=0.25, tol_frac=1e-3,
                 steps=None):
        super().__init__(vocs, variables, objective)
        steps = steps or {}
        # per-axis step: an explicit ABSOLUTE value per axis overrides the fraction
        # of that axis' range (so each motor can get its own probe distance).
        self._step = {}
        for v in variables:
            rng = vocs.variables[v].high - vocs.variables[v].low
            self._step[v] = float(steps[v]) if v in steps else init_step_frac * rng
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
                 n_samples=5, r2_gate=0.9, span_frac=0.0):
        super().__init__(vocs, variables, objective)
        if len(variables) != 1:
            raise ValueError("SurrogateFit (MVP) supports exactly one variable")
        self.var = variables[0]
        self.model = model
        self.n_samples = n_samples
        self.r2_gate = r2_gate
        # span_frac == 0 → sample the full absolute range. > 0 → a RELATIVE window
        # of that fraction, centred on the start point (no absolute origin needed).
        self.span_frac = span_frac
        self._xs: list[float] = []
        self._ys: list[float] = []          # raw objective values
        self._design: list[float] = []      # planned sample abscissae
        self._proposed: Optional[float] = None
        self._pending_val: float = 0.0

    def _build_design(self) -> None:
        v = self.vocs.variables[self.var]
        if self.span_frac and self.span_frac > 0:
            half = 0.5 * self.span_frac * (v.high - v.low)
            c = self._base[self.var]
            lo, hi = v.clip(c - half), v.clip(c + half)
        else:
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
                sigma = res.params["sigma"].value
                info = (f"高斯拟合 {self.objective}({self.var}) ≈ 峰@{self.var}={x_star:.4g}, "
                        f"σ={sigma:.4g}  (R²={getattr(res,'rsquared',1.0):.3f})")
            else:  # quadratic
                mod = QuadraticModel()
                res = mod.fit(ys, mod.guess(ys, x=xs), x=xs)
                a = res.params["a"].value
                b = res.params["b"].value
                c = res.params["c"].value
                if abs(a) < 1e-12:
                    raise ValueError("degenerate quadratic")
                x_star = -b / (2 * a)
                info = (f"二次拟合 {self.objective} ≈ {a:.4g}·{self.var}² + {b:.4g}·{self.var} "
                        f"+ {c:.4g}  → 峰@{self.var}={x_star:.4g}  (R²={getattr(res,'rsquared',1.0):.3f})")
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
        self.fit_info = info
        self._proposed = v.clip(x_star)     # extrapolation clip


class GridScan(Generator):
    """Phase-1 'find light': raster scan over the controlled variables.

    Covers both requested first-light strategies:
      * grid search  -> 2+ variables, N points per axis (raster / serpentine)
      * line search  -> exactly 1 variable, N points along it

    One point per ask(). Best point is tracked; the stage normally stops the
    moment a threshold is crossed (orchestrator's `stop.target`, e.g.
    "y1 > first_light"), otherwise it stops when the grid is exhausted.
    """

    def __init__(self, vocs, variables, objective, n_per_axis=7, span_frac=0.0):
        super().__init__(vocs, variables, objective)
        self.n_per_axis = n_per_axis
        # span_frac == 0 → absolute full-range raster (the default). span_frac > 0 →
        # a RELATIVE window of that fraction of each axis' range, centred on the
        # current operating point (start point). Physically there is no absolute
        # stage origin, so a local scan around where we already are is what a real
        # alignment does; the window is clipped into the variable bounds.
        self.span_frac = span_frac
        self._grid: list[dict[str, float]] = []

    def _build_grid(self) -> None:
        import itertools

        axes = []
        for v in self.variables:
            var = self.vocs.variables[v]
            n = self.n_per_axis
            if self.span_frac and self.span_frac > 0:          # relative window around base
                half = 0.5 * self.span_frac * (var.high - var.low)
                c = self._base[v]
                lo, hi = var.clip(c - half), var.clip(c + half)
            else:                                              # absolute full range
                lo, hi = var.low, var.high
            axes.append([lo + (hi - lo) * i / (n - 1) for i in range(n)])
        self._grid = [dict(zip(self.variables, combo)) for combo in itertools.product(*axes)]

    def ask(self) -> dict[str, float]:
        if not self._grid and self.best_x is None:
            self._build_grid()
        pt = dict(self._base)
        if self._grid:
            pt.update(self._grid.pop(0))
        if not self._grid:              # last point of the grid
            self.done = True
        return pt

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)


class NelderMead(Generator):
    """Downhill-simplex local optimisation, ask/tell driven.

    Standard Nelder-Mead (reflect / expand / contract / shrink). We maximise
    `score`, so internally we minimise cost = -score. One evaluation per ask();
    points are clipped into the variable bounds.
    """

    def __init__(self, vocs, variables, objective,
                 init_step_frac=0.1, tol_frac=1e-3, max_evals=400, init_steps=None):
        super().__init__(vocs, variables, objective)
        self.dim = len(variables)
        init_steps = init_steps or {}
        # initial simplex edge per axis: an explicit ABSOLUTE value per axis overrides
        # the fraction of that axis' range (each axis can seed its own simplex size).
        self._init_step = {}
        for v in variables:
            rng = vocs.variables[v].high - vocs.variables[v].low
            self._init_step[v] = float(init_steps[v]) if v in init_steps else init_step_frac * rng
        self._tol = tol_frac * min(
            vocs.variables[v].high - vocs.variables[v].low for v in variables
        )
        self.max_evals = max_evals
        self._n = 0
        # simplex: list of [vector, cost]; cost None until evaluated
        self._simplex: list[list] = []
        self._phase = "init"
        self._pending_vec: Optional[list[float]] = None
        self._init_idx = 0
        self._xr = self._xe = self._xc = None
        self._fr = None

    # -- vector <-> dict helpers (clipped) --
    def _to_dict(self, vec: list[float]) -> dict[str, float]:
        return {v: self.vocs.variables[v].clip(vec[i]) for i, v in enumerate(self.variables)}

    def _base_vec(self) -> list[float]:
        return [self._base[v] for v in self.variables]

    def _centroid(self) -> list[float]:
        # centroid of all but the worst (simplex is kept sorted, worst last)
        best_n = self._simplex[:-1]
        return [sum(p[0][i] for p in best_n) / len(best_n) for i in range(self.dim)]

    def _reflect_from(self, xo, factor):
        worst = self._simplex[-1][0]
        return [xo[i] + factor * (xo[i] - worst[i]) for i in range(self.dim)]

    def ask(self) -> dict[str, float]:
        if self._phase == "init" and not self._simplex and self._pending_vec is None:
            # queue the initial simplex: base + one perturbation per axis
            base = self._base_vec()
            self._init_vecs = [list(base)]
            for i, v in enumerate(self.variables):
                pert = list(base)
                pert[i] = self.vocs.variables[v].clip(pert[i] + self._init_step[v])
                self._init_vecs.append(pert)
        if self._phase == "init":
            self._pending_vec = self._init_vecs[self._init_idx]
            return self._to_dict(self._pending_vec)
        self._pending_vec = {"reflect": self._xr, "expand": self._xe,
                             "contract": self._xc}[self._phase]
        return self._to_dict(self._pending_vec)

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._n += 1
        cost = -score
        vec = [x[v] for v in self.variables]

        if self._phase == "init":
            self._simplex.append([vec, cost])
            self._init_idx += 1
            if len(self._simplex) == self.dim + 1:
                self._start_iteration()
            return

        if self._phase == "reflect":
            self._fr = cost
            f_best = self._simplex[0][1]
            f_secondworst = self._simplex[-2][1]
            if cost < f_best:
                self._xe = self._reflect_from(self._centroid(), 2.0)  # expand
                self._phase = "expand"
            elif cost < f_secondworst:
                self._accept(vec, cost); self._start_iteration()
            else:
                xo = self._centroid()
                if cost < self._simplex[-1][1]:            # outside contraction
                    self._xc = [xo[i] + 0.5 * (self._xr[i] - xo[i]) for i in range(self.dim)]
                else:                                       # inside contraction
                    worst = self._simplex[-1][0]
                    self._xc = [xo[i] + 0.5 * (worst[i] - xo[i]) for i in range(self.dim)]
                self._phase = "contract"
            return

        if self._phase == "expand":
            if cost < self._fr:
                self._accept(vec, cost)
            else:
                self._accept(self._xr, self._fr)
            self._start_iteration()
            return

        if self._phase == "contract":
            if cost < self._fr:
                self._accept(vec, cost)
                self._start_iteration()
            else:
                self._shrink()               # replace all but best towards best
                self._start_iteration()
            return

    def _accept(self, vec, cost):
        self._simplex[-1] = [list(vec), cost]

    def _shrink(self):
        best = self._simplex[0][0]
        for k in range(1, len(self._simplex)):
            p = self._simplex[k][0]
            shrunk = [best[i] + 0.5 * (p[i] - best[i]) for i in range(self.dim)]
            # cost is stale after shrink; recompute lazily via a cheap re-eval next round
            self._simplex[k] = [shrunk, self._simplex[k][1]]

    def _start_iteration(self):
        self._simplex.sort(key=lambda p: p[1])
        # convergence: simplex geometric size small, or eval budget hit
        size = max(
            abs(self._simplex[0][0][i] - self._simplex[-1][0][i]) for i in range(self.dim)
        )
        if size < self._tol or self._n >= self.max_evals:
            self.done = True
            return
        self._xr = self._reflect_from(self._centroid(), 1.0)
        self._phase = "reflect"


class FormulaMethod(Generator):
    """公式法: analytic 3-point parabolic peak (no least-squares fitting).

    Single variable. Samples exactly 3 points, then computes the peak position
    from the closed-form parabolic-interpolation formula
        x* = x1 + 0.5*h*(y0 - y2) / (y0 - 2*y1 + y2)
    where the samples are equally spaced by h. Distinct from quadratic-fit:
    no regression, no R^2 — an exact formula through the 3 measured points.

    Guardrails: the curvature must be concave (a maximum) for a valid peak;
    otherwise self.failed is set (orchestrator can fall back). x* is clipped.
    """

    def __init__(self, vocs, variables, objective, span_frac=0.5):
        super().__init__(vocs, variables, objective)
        if len(variables) != 1:
            raise ValueError("FormulaMethod supports exactly one variable")
        self.var = variables[0]
        self.span_frac = span_frac
        self._xs: list[float] = []
        self._ys: list[float] = []
        self._design: list[float] = []
        self._proposed: Optional[float] = None

    def ask(self) -> dict[str, float]:
        var = self.vocs.variables[self.var]
        if not self._design and not self._xs:            # build the 3-point design
            c = self._base[self.var]
            h = self.span_frac * 0.5 * (var.high - var.low)
            self._design = [var.clip(c - h), var.clip(c), var.clip(c + h)]
        pt = dict(self._base)
        if self._design:
            pt[self.var] = self._design.pop(0)
        elif self._proposed is not None:
            pt[self.var] = self._proposed
            self._proposed = None
            self.done = True
        else:
            self._compute()
            pt[self.var] = self._proposed if self._proposed is not None else self._base[self.var]
        return pt

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._xs.append(x[self.var])
        self._ys.append(score)

    def _compute(self) -> None:
        x0, x1, x2 = self._xs[:3]
        y0, y1, y2 = self._ys[:3]
        denom = y0 - 2 * y1 + y2
        if denom >= 0 or abs(denom) < 1e-12:      # not concave -> no interior max
            self.failed = True
            self.done = True
            self._proposed = None
            return
        h = x1 - x0
        x_star = x1 + 0.5 * h * (y0 - y2) / denom
        self.fit_info = (f"三点解析(抛物线插值) {self.objective}({self.var})：峰@{self.var}="
                         f"{self.vocs.variables[self.var].clip(x_star):.4g}")
        self._proposed = self.vocs.variables[self.var].clip(x_star)


class ParametricFit(Generator):
    """公式法 / 非标拟合: fit a chosen model with user-KNOWN parameters pinned.

    The distinction from SurrogateFit (a plain free fit) is that the user brings
    domain knowledge and *pins* some parameters:
      * a known vertex / peak location
      * a known optical spot width sigma (a device characteristic)
      * a known curvature
    Only the remaining parameters are fitted — fewer points, more robust, and
    with enough pinned it collapses to an exact "formula". Customers can also
    pass an arbitrary custom model expression, so scenario-specific formulas
    plug in without changing the platform.

    Pipeline usage (single variable):
        algorithm: parametric_fit
        model: gaussian                       # or quadratic, or a custom expr:
        #   model: "amp*exp(-((x-center)**2)/(2*sigma**2)) + offset"
        fixed: {sigma: 0.25}                  # 已知参数钉死 (lmfit vary=False)
        hints: {center: {value: 0.5, min: 0, max: 1.5}}   # 可选初值/边界

    The optimum is read off by evaluating the *fitted* model on a dense grid
    inside the variable range and taking the argmax — works for any model form
    and is inherently range-clipped. R^2 gate + fallback as usual.
    """

    def __init__(self, vocs, variables, objective, model="quadratic",
                 fixed=None, hints=None, n_samples=5, r2_gate=0.9, span_frac=0.0):
        super().__init__(vocs, variables, objective)
        if len(variables) != 1:
            raise ValueError("ParametricFit (MVP) supports exactly one variable")
        self.var = variables[0]
        self.model = model
        self.fixed = dict(fixed or {})
        self.hints = dict(hints or {})
        self.n_samples = n_samples
        self.r2_gate = r2_gate
        self.span_frac = span_frac              # 0 = full range; >0 = window around start
        self._xs: list[float] = []
        self._ys: list[float] = []
        self._design: list[float] = []
        self._proposed: Optional[float] = None

    def _build_design(self) -> None:
        v = self.vocs.variables[self.var]
        if self.span_frac and self.span_frac > 0:
            half = 0.5 * self.span_frac * (v.high - v.low)
            c = self._base[self.var]
            lo, hi = v.clip(c - half), v.clip(c + half)
        else:
            lo, hi = v.low, v.high
        self._design = [lo + (hi - lo) * i / (self.n_samples - 1)
                        for i in range(self.n_samples)]

    def ask(self) -> dict[str, float]:
        if not self._design and not self._xs:
            self._build_design()
        if self._design:
            val = self._design.pop(0)
        elif self._proposed is not None:
            val = self._proposed
            self._proposed = None
            self.done = True
        else:
            self._fit_and_propose()
            val = self._proposed if self._proposed is not None \
                else self.vocs.variables[self.var].clip(self._base[self.var])
        pt = dict(self._base)
        pt[self.var] = val
        return pt

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._xs.append(x[self.var])
        self._ys.append(score)

    def _build_model(self):
        from lmfit.models import ExpressionModel, GaussianModel, QuadraticModel

        if self.model == "quadratic":
            return QuadraticModel()
        if self.model == "gaussian":
            return GaussianModel()
        return ExpressionModel(self.model)     # custom, non-standard formula

    def _fit_and_propose(self) -> None:
        import numpy as np

        xs = np.asarray(self._xs, float)
        ys = np.asarray(self._ys, float)       # score space (higher = better)
        v = self.vocs.variables[self.var]
        try:
            mod = self._build_model()
            try:
                params = mod.guess(ys, x=xs)   # builtin models can guess
            except (NotImplementedError, AttributeError, Exception):
                params = mod.make_params()     # custom expression: start from hints
            for name, spec in self.hints.items():      # user initial values / bounds
                if name in params:
                    params[name].set(**spec)
            for name, val in self.fixed.items():       # pin KNOWN parameters
                if name in params:
                    params[name].set(value=val, vary=False)
            res = mod.fit(ys, params, x=xs)
            r2 = getattr(res, "rsquared", 1.0)
            # read the optimum off the fitted curve (range-clipped by construction)
            xg = np.linspace(v.low, v.high, 501)
            yg = np.asarray(res.eval(x=xg), float)
            x_star = float(xg[int(np.argmax(yg))])
            free = ", ".join(f"{n}={p.value:.4g}" for n, p in res.params.items() if p.vary)
            pinned = ", ".join(f"{n}={v2}" for n, v2 in self.fixed.items())
            info = f"参数拟合[{self.model}] {self.objective}({self.var})：{free or '—'}"
            if pinned:
                info += f"；钉死 {pinned}"
            info += f"  → 峰@{self.var}={x_star:.4g}  (R²={r2:.3f})"
        except Exception:
            self.failed = True
            self.done = True
            self._proposed = None
            return
        if r2 < self.r2_gate:
            self.failed = True
            self.done = True
            self._proposed = None
            return
        self.fit_info = info
        self._proposed = v.clip(x_star)


class GradientAscent(Generator):
    """梯度上升 (PI 'lightning' 式局部对准).

    Emulates a fast gradient alignment: at the current point it probes a tiny
    step on each axis to estimate the local gradient by finite differences,
    then takes a step along the (normalised) ascent direction. A trial that
    improves is accepted and the step grows; a trial that fails shrinks the
    step and retries. Converges when the step falls below tolerance.

    Multi-variable, one evaluation per ask(), all points clipped into bounds.
    Derivative-free at the interface — the 'gradient' is measured, exactly like
    a real gradient-alignment routine on hardware.
    """

    def __init__(self, vocs, variables, objective,
                 probe_frac=0.02, step_frac=0.15, tol_frac=1e-3, max_line=6):
        super().__init__(vocs, variables, objective)
        self.range = {v: vocs.variables[v].high - vocs.variables[v].low for v in variables}
        self.h = {v: probe_frac * self.range[v] for v in variables}
        self.lr = step_frac
        self.tol = tol_frac
        self.max_line = max_line
        self._phase = "base"
        self._base_val: Optional[float] = None
        self._dir: dict[str, float] = {}
        self._probe_i = 0
        self._slope: dict[str, float] = {}
        self._line_tries = 0

    def _clip(self, v, val):
        return self.vocs.variables[v].clip(val)

    def ask(self) -> dict[str, float]:
        if self._phase == "base":
            return dict(self._base)
        if self._phase == "probe":
            v = self.variables[self._probe_i]
            val = self._clip(v, self._base[v] + self.h[v])
            if val == self._base[v]:                      # at the upper wall → probe back
                val = self._clip(v, self._base[v] - self.h[v])
            cand = dict(self._base); cand[v] = val
            return cand
        # line: step along the ascent direction
        return {v: self._clip(v, self._base[v] + self.lr * self._dir.get(v, 0.0) * self.range[v])
                for v in self.variables}

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        if self._phase == "base":
            self._base = {v: x[v] for v in self.variables}
            self._base_val = score
            self._probe_i = 0; self._slope = {}; self._phase = "probe"
            return
        if self._phase == "probe":
            v = self.variables[self._probe_i]
            dx = x[v] - self._base[v]
            self._slope[v] = (score - self._base_val) / dx if abs(dx) > 1e-12 else 0.0
            self._probe_i += 1
            if self._probe_i >= len(self.variables):
                # gradient in normalised coords; normalise to a unit direction
                gn = {v: self._slope[v] * self.range[v] for v in self.variables}
                mag = math.sqrt(sum(g * g for g in gn.values()))
                if mag < 1e-9:
                    self.done = True
                    return
                self._dir = {v: gn[v] / mag for v in self.variables}
                self._phase = "line"; self._line_tries = 0
            return
        # line result
        if score > self._base_val + 1e-12:                # improved → accept, grow step
            self._base = {v: x[v] for v in self.variables}
            self._base_val = score
            self.lr = min(self.lr * 1.5, 0.5)
            self._probe_i = 0; self._slope = {}; self._phase = "probe"
        else:                                             # no gain → shrink & retry
            self.lr *= 0.5; self._line_tries += 1
            if self.lr < self.tol or self._line_tries >= self.max_line:
                self.done = True


class DampedSensitivity(Generator):
    """阻尼灵敏度求解: multi-in / multi-out target solving via damped least squares.

    You know the sensitivity (Jacobian) S = ∂y/∂x of a set of measured outputs y
    (n_y of them) w.r.t. a set of actuators x (n_x of them), and a target value for
    each y. Each round measures y, forms the residual Δy = y_target − y_meas, and
    steps the actuators by

        Δx = damping · S⁺_λ · Δy ,      x ← clip(x + Δx)

    where S⁺_λ is a **regularised (Tikhonov / damped-SVD) pseudo-inverse** rather
    than a raw `pinv`: via the SVD S = U diag(σ) Vᵀ,

        S⁺_λ Δy = Σ_i  σ_i / (σ_i² + λ) · (u_iᵀ Δy) · v_i .

    This is the numerically-stable form used in beam-steering / adaptive-optics
    controllers — it stays bounded when S is singular, ill-conditioned, tall, wide
    or rank-deficient (a plain pinv blows up on the small singular values). The
    `damping` ratio (0<d≤1) additionally under-relaxes the step so a locally-linear
    model tracks a genuinely non-linear plant without overshoot.

    Dimensions are free: the node picks which x it drives and which y it targets;
    `sensitivity` and `targets` just have to match those. Iterates until the
    residual norm falls below `tol` or `max_solves` steps are taken.
    """

    def __init__(self, vocs, variables, objective, sensitivity=None, targets=None,
                 damping=0.5, reg=1e-6, tol=1e-4, max_solves=40):
        super().__init__(vocs, variables, objective)
        # target outputs: explicit {y: value}, else every TARGET-mode VOCS objective
        if targets:
            self.targets = {k: float(v) for k, v in targets.items()}
        else:
            self.targets = {n: float(o.target) for n, o in vocs.objectives.items()
                            if o.target is not None}
        if not self.targets:
            raise ValueError("DampedSensitivity needs targets (y→目标值) — none given "
                             "and no TARGET-mode objectives in VOCS")
        self.objs = list(self.targets)                 # row order of S
        self.damping = float(damping)
        self.reg = float(reg)
        self.tol = float(tol)
        self.max_solves = int(max_solves)
        self._S = self._as_matrix(sensitivity)         # n_y × n_x
        self._next: Optional[dict[str, float]] = None
        self._solves = 0

    def channels(self) -> set:
        """Objective channels this solver must read every step (all its targets)."""
        return set(self.objs)

    def _as_matrix(self, sens) -> list[list[float]]:
        """Accept a nested dict {y:{x:val}} OR a plain 2-D list, ordered to
        (self.objs × self.variables). Missing entries default to 0."""
        rows = []
        if isinstance(sens, dict):
            for o in self.objs:
                row = sens.get(o, {}) or {}
                rows.append([float(row.get(v, 0.0)) for v in self.variables])
        elif sens:                                     # list-of-lists / rows
            for i, o in enumerate(self.objs):
                r = list(sens[i]) if i < len(sens) else []
                rows.append([float(r[j]) if j < len(r) else 0.0
                             for j in range(len(self.variables))])
        else:
            raise ValueError("DampedSensitivity needs a `sensitivity` matrix (∂y/∂x)")
        return rows

    def set_base(self, point: dict[str, float]) -> None:
        super().set_base(point)
        self._next = dict(self._base)

    def ask(self) -> dict[str, float]:
        if self._next is None:
            self._next = dict(self._base)
        return dict(self._next)

    def _solve(self, resid) -> list[float]:
        """Δx = damped-SVD pseudo-inverse of S applied to the residual Δy."""
        import numpy as np
        S = np.asarray(self._S, float)
        dy = np.asarray(resid, float)
        u, s, vt = np.linalg.svd(S, full_matrices=False)
        # damped inverse singular values: σ/(σ²+λ) — bounded even as σ→0
        d = s / (s * s + self.reg)
        dx = vt.T @ (d * (u.T @ dy))
        return [float(v) for v in dx]

    def observe(self, x: dict[str, float], y: dict[str, float]) -> None:
        import numpy as np
        self._solves += 1
        resid = [self.targets[o] - float(y.get(o, 0.0)) for o in self.objs]
        err = float(np.linalg.norm(resid))
        score = -err                                   # smaller residual = better
        if score > self.best_score:
            self.best_score = score
            self.best_x = {v: x[v] for v in self.variables}
        self.fit_info = ("阻尼灵敏度求解：残差‖Δy‖=%.3g，目标 %s（阻尼 d=%.2g, λ=%.1g）"
                         % (err, ", ".join(f"{o}→{self.targets[o]:.4g}" for o in self.objs),
                            self.damping, self.reg))
        if err < self.tol or self._solves >= self.max_solves:
            self.done = True
            return
        dx = self._solve(resid)
        nxt = {}
        for i, v in enumerate(self.variables):
            nxt[v] = self.vocs.variables[v].clip(x[v] + self.damping * dx[i])
        self._next = nxt

    def tell(self, x: dict[str, float], score: float) -> None:
        # best-x / stopping are handled in observe() with the full y vector; the
        # scalar score path is intentionally a no-op here.
        return


class SensitivityScan(Generator):
    """灵敏度采集（单轴扫描 → 曲线 + 灵敏度矩阵）。

    不是优化器：它**测量**局部响应。以当前操作点为原点，把每个自变量单独走
    n_points 个点（其余轴钉在原点），记录每个因变量，然后对每对 x→y 拟合出
    原点处斜率 ∂y/∂x，装配成矩阵 —— 形状与 `DampedSensitivity` 的 `sensitivity`
    入参一致，所以流程里可以「先测灵敏度 → 再喂给阻尼灵敏度求解」。

    两个刻意的设计：
      * `observe()` 拿整条 y 向量，所以一次扫描同时标定所有被选中的因变量；
      * 原点被列为第一个采样点且记为 best_x，于是执行核在阶段结束时把操作点
        （以及真实机构）**送回原点** —— 采集不应该把台架留在最后一个扫描点。

    多原点采集、曲线作图与线性范围分析走 `POST /sensitivity` 与画布的
    『📐 灵敏度采集』面板（同一套 `optplat.sensitivity` 核心）。
    """

    def __init__(self, vocs, variables, objective, step=0.1, steps=None,
                 n_points=5, fit="linear", linear_tol=0.05, objectives=""):
        super().__init__(vocs, variables, objective)
        from .sensitivity import SensitivitySpec, fit_curve       # 避免循环导入
        self._fit_curve = fit_curve
        picked = [o.strip() for o in str(objectives or "").split(",") if o.strip()]
        self.objs = [o for o in (picked or list(vocs.objectives)) if o in vocs.objectives]
        self.spec = SensitivitySpec(variables=list(variables), objectives=self.objs,
                                    step=float(step), steps=steps or {},
                                    n_points=int(n_points), fit=str(fit),
                                    linear_tol=float(linear_tol))
        self._plan: list[tuple[str, float]] = []                  # (轴, 绝对坐标)
        self._i = 0
        self._data: dict[str, dict] = {}
        self.matrix: dict[str, dict] = {}
        self.fits: dict[str, dict] = {}

    def channels(self) -> set:
        """本算子每步都要读的通道 = 所有被标定的因变量。"""
        return set(self.objs)

    def set_base(self, point):
        super().set_base(point)
        from .sensitivity import _offsets
        self.origin = dict(self._base)
        self._plan = [(self.variables[0], self.origin[self.variables[0]])]   # 原点先测
        for v in self.variables:
            var = self.vocs.variables[v]
            for off in _offsets(self.spec, v):
                if abs(off) < 1e-12:
                    continue                                      # 原点已在计划里
                self._plan.append((v, var.clip(self.origin[v] + off)))
        self._data = {v: {"xs": [], "y": {o: [] for o in self.objs}} for v in self.variables}

    def ask(self) -> dict[str, float]:
        v, val = self._plan[min(self._i, len(self._plan) - 1)]
        return {**self.origin, v: val}

    def observe(self, x: dict[str, float], y: dict[str, float]) -> None:
        v, val = self._plan[min(self._i, len(self._plan) - 1)]
        if self._i == 0:                       # 原点：计入每一条曲线
            for axis in self.variables:
                self._data[axis]["xs"].append(self.origin[axis])
                for o in self.objs:
                    self._data[axis]["y"][o].append(float(y.get(o, float("nan"))))
        else:
            self._data[v]["xs"].append(val)
            for o in self.objs:
                self._data[v]["y"][o].append(float(y.get(o, float("nan"))))
        self._i += 1
        if self._i >= len(self._plan):
            self._finish()
            self.done = True

    def _finish(self) -> None:
        order = 2 if str(self.spec.fit).startswith("quad") else 1
        self.fits = {o: {} for o in self.objs}
        self.matrix = {o: {} for o in self.objs}
        for v in self.variables:
            for o in self.objs:
                f = self._fit_curve(self._data[v]["xs"], self._data[v]["y"][o],
                                    self.origin[v], order=order,
                                    linear_tol=self.spec.linear_tol)
                self.fits[o][v] = f
                self.matrix[o][v] = f["slope"]
        cells = ", ".join(f"∂{o}/∂{v}={self.matrix[o][v]:.4g}"
                          for o in self.objs for v in self.variables)
        worst = min((self.fits[o][v]["r2"] for o in self.objs for v in self.variables),
                    default=1.0)
        self.fit_info = f"灵敏度矩阵：{cells}（最差线性度 R²={worst:.3f}）"

    def tell(self, x: dict[str, float], score: float) -> None:
        # 采集不寻优。把原点记为 best，好让执行核在阶段结束时把操作点送回原点。
        if self._i <= 1 and score > self.best_score:
            self.best_score = score
            self.best_x = {v: self.origin[v] for v in self.variables}


class SpiralScan(Generator):
    """螺旋扫描（找光）：**从原点向外**螺旋式展开搜索。

    真实台架没有绝对坐标，找光都是"从当前位置开始往外找"。阿基米德螺旋正是光纤耦合
    对准的经典搜法：半径随角度线性增长，先密集搜近处、再逐圈扩大，配合阶段的
    `stop.target` 一旦扫到阈值就停——比整幅网格扫描省得多。

    **变量个数任意**（不同场景加载的 x 个数不同，节点不写死维度）：
      * 1 个变量 → 从原点交替 ±r 向两侧外扩（退化成展开式线搜索）；
      * 2 个变量 → 标准阿基米德螺旋，每 `points_per_turn` 个点转一圈，半径每圈 +step；
      * ≥3 个变量 → 同样的"半径线性增长"，方向取自 N 维单位球面上的低差异序列
        （Roberts/Kronecker + 正态反函数），即从原点向外的均匀扩散搜索。

    每轴步距可各不相同（`steps`：{x1:Δ1, x2:Δ2, ...}），因为不同执行器的物理量纲和
    行程差异很大；留空则该轴用统一 `step`。
    """

    def __init__(self, vocs, variables, objective, step=0.1, steps=None,
                 points_per_turn=12, turns=4):
        super().__init__(vocs, variables, objective)
        self.step = float(step)
        self.steps = dict(steps or {})
        self.ppt = max(2, int(points_per_turn))
        self.turns = max(1, int(turns))
        self.n_points = self.ppt * self.turns
        self._k = 0

    def _axis_step(self, v: str) -> float:
        s = self.steps.get(v)
        return float(s) if s not in (None, "") else self.step

    @staticmethod
    def _sphere_dirs(n_dim: int, k: int) -> list[float]:
        """N 维单位球面上的第 k 个低差异方向（确定性，无随机种子依赖）。

        Roberts 序列（广义黄金比）给 [0,1)^N 的低差异点 → 正态分布反函数 → 归一化，
        得到球面上近似均匀且互不聚集的方向序列。"""
        import numpy as np
        from scipy.special import ndtri
        g = 2.0                                   # 解 g^(d+1) = g + 1 得广义黄金比
        for _ in range(24):
            g = (1.0 + g) ** (1.0 / (n_dim + 1.0))
        alpha = np.array([g ** -(i + 1) for i in range(n_dim)])
        u = np.mod(0.5 + alpha * (k + 1), 1.0)
        u = np.clip(u, 1e-9, 1 - 1e-9)
        z = ndtri(u)
        nrm = float(np.linalg.norm(z))
        return (z / nrm).tolist() if nrm > 0 else [1.0] + [0.0] * (n_dim - 1)

    def _direction(self, k: int) -> list[float]:
        n = len(self.variables)
        if n == 1:
            return [1.0 if k % 2 == 0 else -1.0]
        if n == 2:
            import math
            th = 2.0 * math.pi * k / self.ppt
            return [math.cos(th), math.sin(th)]
        return self._sphere_dirs(n, k)

    def ask(self) -> dict[str, float]:
        k = self._k
        # 半径线性增长：转满一圈半径长一个 step（1 维时每两点长一个 step）
        r = (k + 1) / self.ppt if len(self.variables) > 1 else (k // 2 + 1)
        d = self._direction(k)
        out = {}
        for i, v in enumerate(self.variables):
            val = self._base[v] + r * self._axis_step(v) * d[i]
            out[v] = self.vocs.variables[v].clip(val)
        return out

    def tell(self, x: dict[str, float], score: float) -> None:
        self._record(x, score)
        self._k += 1
        if self._k >= self.n_points:
            self.done = True
