"""run_workflow — one place to run a graph IR against a simulated bench.

Shared by the MCP server and the compare/autotune helpers so "跑一个方案" means
exactly the same thing everywhere: build the demo VOCS, pick the bench function,
wrap it in the (optionally noisy / safety-limited) evaluator, run the
GraphRunner, and return a compact, JSON-friendly summary.

Reuses the exact engine + safety guardrails the API and canvas use — an agent
driving this through MCP can never command out-of-range motion.
"""
from __future__ import annotations

from typing import Optional

from .demo import bench_func, demo_vocs
from .evaluator import Evaluator
from .graph import GraphRunner
from .hardware import HardwareEvaluator, SafetyLimits, SimulatedMeter, SimulatedStage
from .vocs import VOCS


def _build_evaluator(vocs: VOCS, bench: str, noise: float, averages: int,
                     safety: bool, safety_limits: Optional[dict], seed: int = 0):
    func = bench_func(bench)
    costs = {n: o.cost for n, o in vocs.objectives.items()}
    groups = {n: o.group for n, o in vocs.objectives.items() if o.group}
    if noise > 0 or averages > 1 or safety:
        stage = SimulatedStage(vocs.initial_point())
        meter = SimulatedMeter(stage, func, noise=noise, seed=seed)
        limits = None
        if safety:
            if safety_limits:
                lims = {n: (float(lo), float(hi)) for n, (lo, hi) in safety_limits.items()}
            else:
                lims = {n: (v.low, v.high) for n, v in vocs.variables.items()}
            limits = SafetyLimits(lims)
        return HardwareEvaluator(stage, meter, averages=averages, safety=limits,
                                 costs=costs, groups=groups)
    return Evaluator(func, costs=costs, groups=groups)


def run_workflow(graph: dict, bench: str = "single_peak", noise: float = 0.0,
                 averages: int = 1, safety: bool = False,
                 safety_limits: Optional[dict] = None, eval_budget: int = 5000,
                 start_point: Optional[dict[str, float]] = None,
                 seed: int = 0, keep_history: bool = False) -> dict:
    """Run a {nodes, edges, until?} graph on a simulated bench; return a summary.

    `keep_history` includes the full per-evaluation trace — needed by trace_digest
    for diagnosis, but omitted by default because it is large. `seed` varies the
    noise realisation so a caller can repeat a run across seeds (the digest_many
    "does this defect actually reproduce?" check).
    """
    vocs = demo_vocs()
    ev = _build_evaluator(vocs, bench, noise, averages, safety, safety_limits, seed)
    res = GraphRunner(vocs, ev, graph, eval_budget=eval_budget,
                      start_point=start_point).run()
    out = {
        "bench": bench,
        "state": res["state"],
        "objectives": res["objectives"],
        "n_evals": res["n_evals"],
        "sim_seconds": round(res.get("sim_seconds", 0.0), 3),
        "reads": res.get("reads", {}),
        "fits": res.get("fits", {}),
        "events": res["events"],
    }
    if keep_history:
        out["history"] = res.get("history", [])
    return out
