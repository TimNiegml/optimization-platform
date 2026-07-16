"""AutoTuner (L1) = deterministic meta-optimizer over algorithm workflows.

Searches the space of workflow IRs (粗调 → 精调 → 拟合 three-phase pipelines)
and ranks them by a multi-objective utility over **quality / time / stability**,
exactly the axes the user cares about:

  quality   final objective value (mean over trials)
  time      sim_seconds — the measurement cost model (mean over trials)
  stability 达标率 (if a target is given) or 1 - CV(quality) — from repeating each
            candidate over several noise realisations / seeds (噪声变异)

Candidate space = three-phase combos × variations:
  * param variation  — a few 档位 per algorithm (incl. analytic/fit params)
  * noise variation  — repeat under several σ / seeds → robustness
  * model variation  — the evaluator comes from a ModelProvider, so an analytic
                       bench OR a data-driven surrogate feeds the same search.

No LLM. Reuses GraphRunner + the existing safety guardrails. This is also the
implementation body of the future MCP `autotune` tool.
"""
from __future__ import annotations

import itertools
import statistics
from typing import Optional

from asteval import Interpreter
from pydantic import BaseModel, Field

from .demo import demo_vocs
from .evaluator import Evaluator
from .graph import GraphRunner
from .hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage
from .models import build_model
from .registry import REGISTRY
from .vocs import VOCS

# Which registry `category` belongs to which pipeline phase. A NEW algorithm
# only has to declare its category — it then joins the right phase automatically,
# so the variation set is never hard-wired to a fixed algorithm list.
CATEGORY_PHASE = {
    "find-light": "coarse", "bayesian": "coarse",
    "local": "refine",
    "fit": "fit", "analytic": "fit",
}


def algo_phase(algo: str) -> Optional[str]:
    spec = REGISTRY.get(algo)
    return CATEGORY_PHASE.get(spec.category) if spec else None


def phase_algorithms() -> dict[str, list[str]]:
    """Group all registered algorithms into coarse / refine / fit phases."""
    out: dict[str, list[str]] = {"coarse": [], "refine": [], "fit": []}
    for name, spec in REGISTRY.items():
        ph = CATEGORY_PHASE.get(spec.category)
        if ph:
            out[ph].append(name)
    return out


def _param_value_set(meta: dict) -> list:
    """Candidate values for one param, auto-derived from its schema."""
    t = meta.get("type")
    if t == "enum" and meta.get("options"):
        return list(meta["options"])
    if t in ("int", "float"):
        vals = []
        if "min" in meta:
            vals.append(int(meta["min"]) if t == "int" else float(meta["min"]))
        if "max" in meta:
            vals.append(int(meta["max"]) if t == "int" else float(meta["max"]))
        return vals
    return []                                   # str / dict: not auto-enumerable


def default_variation(algo: str) -> list[dict]:
    """Auto-build the 变异档位 for an algorithm from its registry param schema:
    the default (empty = builder defaults) plus one variant per param extreme.
    Any user override in TuneSpec.variation replaces this."""
    spec = REGISTRY.get(algo)
    params = (spec.params if spec else {}) or {}
    variants = [{}]
    for pname, meta in params.items():
        default = meta.get("default")
        for v in _param_value_set(meta):
            if v != default:
                variants.append({pname: v})
    return variants


class TuneSpec(BaseModel):
    bench: str = "single_peak"
    model: Optional[dict] = None                 # ModelProvider spec; overrides bench
    landscape_vars: list[str] = Field(default_factory=lambda: ["x1", "x2"])
    landscape_obj: str = "y1"
    balance_vars: list[str] = Field(default_factory=lambda: ["x3"])
    balance_obj: Optional[str] = "y2"
    target: Optional[str] = "y1>=0.95 and y2>=0.9"   # 达标定义 (None = 纯最大化)
    quality_obj: Optional[str] = None                # default = landscape_obj
    weights: dict = Field(default_factory=lambda: {"quality": 1.0, "time": 0.5, "stability": 1.0})
    noise_levels: list[float] = Field(default_factory=lambda: [0.0, 0.02])
    n_trials: int = 2
    max_candidates: int = 18
    eval_budget: int = 2000
    seed: int = 0
    param_variation: bool = True
    # None = use ALL registered algorithms in that phase (new algorithms included
    # automatically). A list restricts to those algorithms.
    allow_coarse: Optional[list[str]] = None
    allow_refine: Optional[list[str]] = None
    allow_fit: Optional[list[str]] = None
    # per-algorithm 变异档位 override: {algo: [ {param: value, ...}, ... ]}.
    # Absent algorithms fall back to default_variation() from the param schema.
    variation: Optional[dict[str, list[dict]]] = None


def _label(algo: str) -> str:
    spec = REGISTRY.get(algo)
    return (spec.label if spec and spec.label else algo)


def _variants(spec: TuneSpec, algo: str) -> list[dict]:
    if spec.variation and algo in spec.variation:
        return spec.variation[algo] or [{}]
    return default_variation(algo) if spec.param_variation else [{}]


def _phase_options(spec: TuneSpec, phase: str, allow: Optional[list[str]], nvars: int):
    algos = allow if allow is not None else phase_algorithms()[phase]
    opts = [("none", {})]
    for algo in algos:
        s = REGISTRY.get(algo)
        if s is None:
            continue
        if nvars > 1 and s.single_var:            # single-var algo can't drive a multi-var phase
            continue
        for p in _variants(spec, algo):
            opts.append((algo, p))
    return opts


def generate_candidates(spec: TuneSpec) -> list[dict]:
    """Build the candidate workflow graphs (three-phase combos × variation)."""
    coarse = _phase_options(spec, "coarse", spec.allow_coarse, len(spec.landscape_vars))
    refine = _phase_options(spec, "refine", spec.allow_refine, len(spec.landscape_vars))
    fit = _phase_options(spec, "fit", spec.allow_fit, len(spec.balance_vars)) \
        if spec.balance_obj else [("none", {})]

    combos = []
    for c, r, f in itertools.product(coarse, refine, fit):
        if c[0] == "none" and r[0] == "none":     # need at least a coarse or refine phase
            continue
        combos.append((c, r, f))

    # deterministic even-stride down-sample to the budget
    if len(combos) > spec.max_candidates:
        step = len(combos) / spec.max_candidates
        combos = [combos[int(i * step)] for i in range(spec.max_candidates)]

    out = []
    for idx, (c, r, f) in enumerate(combos):
        out.append(_build_candidate(spec, idx, c, r, f))
    return out


def _build_candidate(spec: TuneSpec, idx: int, c, r, f) -> dict:
    nodes, edges, prev, parts = [], [], None, []
    n = 0

    def add(algo, params, variables, objective, extra=None):
        nonlocal n, prev
        n += 1
        nid = f"n{n}"
        stage = f"{_label(algo)}"
        data = {"algorithm": algo, "variables": variables, "objective": objective,
                "stage": stage, **params, **(extra or {})}
        nodes.append({"id": nid, "type": "algorithm", "data": data})
        if prev:
            edges.append({"source": prev, "target": nid})
        prev = nid

    for phase, (algo, params) in (("coarse", c), ("refine", r)):
        if algo == "none":
            continue
        extra = {}
        if phase == "coarse" and algo in ("grid_scan", "line_scan"):
            extra = {"stop": {"target": f"{spec.landscape_obj}>0.2"}}
        add(algo, params, spec.landscape_vars, spec.landscape_obj, extra)
        pl = _label(algo)
        if params:
            pl += "(" + ",".join(f"{k}={v}" for k, v in params.items()) + ")"
        parts.append(pl)
    if f[0] != "none" and spec.balance_obj:
        add(f[0], f[1], spec.balance_vars, spec.balance_obj,
            {"keep": f"{spec.landscape_obj}>0.6"})
        parts.append(_label(f[0]))

    graph = {"nodes": nodes, "edges": edges}
    if spec.target:
        graph["until"] = spec.target
    return {"id": f"c{idx}", "label": " → ".join(parts), "graph": graph}


def _reached(target: Optional[str], res: dict) -> Optional[bool]:
    if not target:
        return None
    a = Interpreter(minimal=True)
    a.symtable.update(res.get("state", {}))
    a.symtable.update(res.get("objectives", {}))
    try:
        return bool(a(target))
    except Exception:
        return False


def _make_evaluator(vocs: VOCS, model_fn, costs, noise: float, seed: int):
    if noise > 0:
        stage = SimulatedStage(vocs.initial_point())
        meter = SimulatedMeter(stage, model_fn, noise=noise, seed=seed)
        return HardwareEvaluator(stage, meter, costs=costs)
    return Evaluator(model_fn, costs=costs)


def evaluate_candidate(spec: TuneSpec, vocs: VOCS, model_fn, costs, graph: dict) -> dict:
    qobj = spec.quality_obj or spec.landscape_obj
    qs, ts, evs, succ = [], [], [], []
    for noise in spec.noise_levels:
        for t in range(spec.n_trials):
            seed = spec.seed + t * 7 + int(noise * 1000)
            try:
                ev = _make_evaluator(vocs, model_fn, costs, noise, seed)
                res = GraphRunner(vocs, ev, graph, eval_budget=spec.eval_budget).run()
            except Exception:
                qs.append(0.0); ts.append(0.0); succ.append(0.0)     # broken candidate scores 0
                continue
            qs.append(float(res["objectives"].get(qobj, 0.0)))
            ts.append(float(res.get("sim_seconds", 0.0)))
            evs.append(float(res.get("n_evals", 0)))
            r = _reached(spec.target, res)
            succ.append(1.0 if r else 0.0)

    q = statistics.fmean(qs) if qs else 0.0
    tm = statistics.fmean(ts) if ts else 0.0
    ev_mean = statistics.fmean(evs) if evs else 0.0
    if spec.target:
        stability = statistics.fmean(succ) if succ else 0.0
    else:
        mean = statistics.fmean(qs) if qs else 0.0
        sd = statistics.pstdev(qs) if len(qs) > 1 else 0.0
        stability = max(0.0, 1.0 - (sd / abs(mean) if mean else 0.0))
    return {"quality": q, "time": tm, "stability": stability, "evals": ev_mean,
            "detail": {"quality_all": qs, "success_rate": (statistics.fmean(succ) if succ else 0.0)}}


def _norm(vals: list[float], higher_better: bool) -> list[float]:
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-12:
        return [0.5 for _ in vals]
    return [((v - lo) / (hi - lo)) if higher_better else (1 - (v - lo) / (hi - lo)) for v in vals]


def _pareto_flags(rows: list[dict]) -> list[bool]:
    flags = []
    for i, a in enumerate(rows):
        dominated = False
        for j, b in enumerate(rows):
            if i == j:
                continue
            if (b["quality"] >= a["quality"] and b["time"] <= a["time"]
                    and b["stability"] >= a["stability"]
                    and (b["quality"] > a["quality"] or b["time"] < a["time"]
                         or b["stability"] > a["stability"])):
                dominated = True
                break
        flags.append(not dominated)
    return flags


def run_autotune(spec: TuneSpec) -> dict:
    vocs = demo_vocs()
    costs = {name: o.cost for name, o in vocs.objectives.items()}
    model_fn = build_model(spec.model) if spec.model else build_model({"kind": "analytic", "bench": spec.bench})

    cands = generate_candidates(spec)
    rows = []
    for c in cands:
        m = evaluate_candidate(spec, vocs, model_fn, costs, c["graph"])
        rows.append({**c, **m})

    if rows:
        qn = _norm([r["quality"] for r in rows], True)
        tn = _norm([r["time"] for r in rows], False)     # lower time → higher goodness
        sn = _norm([r["stability"] for r in rows], True)
        w = spec.weights
        wq, wt, ws = w.get("quality", 1.0), w.get("time", 0.5), w.get("stability", 1.0)
        for r, a, b, c2 in zip(rows, qn, tn, sn):
            r["utility"] = wq * a + wt * b + ws * c2
        for r, pf in zip(rows, _pareto_flags(rows)):
            r["pareto"] = pf
        rows.sort(key=lambda r: r["utility"], reverse=True)

    return {"ranked": rows, "n_candidates": len(rows),
            "spec": spec.model_dump() if hasattr(spec, "model_dump") else spec.dict()}
