"""Audit — deterministic scoring of a solution on DEV vs FROZEN, and the
generalization gap that exposes overfitting.

Design rule this file exists to enforce: **the scorer is not an LLM.** Every number
here comes from running the workflow through the same engine and safety guardrails
as production, on a fingerprinted problem set. An agent may read this report and
render a verdict, but it cannot influence the numbers — otherwise the loop learns
to persuade the judge instead of learning to couple faster.

The central instrument is the **generalization gap**:

    gap = score(DEV) - score(FROZEN)

A solution tuned honestly scores similarly on both. A solution that was tuned
against the visible problems until it memorised them scores well on DEV and badly
on FROZEN — the gap catches that without anyone needing to inspect intent.

Every report embeds both suite fingerprints, so a result can always be re-checked
against the exact problem set it claims to have been measured on.
"""
from __future__ import annotations

import statistics
from typing import Optional

from asteval import Interpreter

from .benchsuite import BenchSuite, Problem, get_suite
from .demo import bench_func, demo_vocs
from .evaluator import Evaluator
from .graph import GraphRunner
from .hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage

# A gap above this (in normalised quality) is treated as overfitting to DEV.
OVERFIT_GAP = 0.10
# Below this frozen success-rate a solution is not fit for acceptance.
MIN_FROZEN_SUCCESS = 0.6


def _reached(target: Optional[str], res: dict) -> bool:
    if not target:
        return True
    a = Interpreter(minimal=True)
    a.symtable.update(res.get("state", {}))
    a.symtable.update(res.get("objectives", {}))
    try:
        return bool(a(target))
    except Exception:
        return False


def _evaluator(vocs, func, problem: Problem):
    costs = {n: o.cost for n, o in vocs.objectives.items()}
    groups = {n: o.group for n, o in vocs.objectives.items() if o.group}
    if problem.noise > 0:
        stage = SimulatedStage(problem.start_point or vocs.initial_point())
        meter = SimulatedMeter(stage, func, noise=problem.noise, seed=problem.seed)
        return HardwareEvaluator(stage, meter, averages=problem.averages,
                                 costs=costs, groups=groups)
    return Evaluator(func, costs=costs, groups=groups)


def run_problem(graph: dict, problem: Problem) -> dict:
    """Run one workflow against one problem. Failures score zero rather than
    raising — a solution that crashes must be ranked, not crash the audit."""
    vocs = demo_vocs()
    func = bench_func(problem.bench)
    try:
        ev = _evaluator(vocs, func, problem)
        res = GraphRunner(vocs, ev, graph, eval_budget=problem.eval_budget,
                          start_point=problem.start_point).run()
    except Exception as e:
        return {"problem": problem.id, "bench": problem.bench, "ok": False,
                "error": f"{type(e).__name__}: {e}", "quality": 0.0,
                "reached": False, "sim_seconds": 0.0, "n_evals": 0}
    q = float(res["objectives"].get(problem.quality_obj, 0.0))
    return {"problem": problem.id, "bench": problem.bench, "ok": True,
            "quality": round(q, 6), "reached": _reached(problem.target, res),
            "sim_seconds": round(float(res.get("sim_seconds", 0.0)), 3),
            "n_evals": int(res.get("n_evals", 0)),
            "objectives": {k: round(float(v), 6) for k, v in res["objectives"].items()},
            "_result": res}


def score_suite(graph: dict, suite: BenchSuite, keep_traces: bool = False) -> dict:
    """Deterministic aggregate score of a workflow over a whole suite."""
    rows = [run_problem(graph, p) for p in suite.problems]
    traces = [r.pop("_result") for r in rows] if not keep_traces else \
             [r.get("_result") for r in rows]
    if not keep_traces:
        pass                                    # traces dropped from the report body
    qualities = [r["quality"] for r in rows]
    return {
        "suite": suite.name, "kind": suite.kind, "fingerprint": suite.fingerprint(),
        "n_problems": len(rows),
        "mean_quality": round(statistics.fmean(qualities), 6) if qualities else 0.0,
        "min_quality": round(min(qualities), 6) if qualities else 0.0,
        "success_rate": round(statistics.fmean(1.0 if r["reached"] else 0.0 for r in rows), 4)
                        if rows else 0.0,
        "mean_sim_seconds": round(statistics.fmean(r["sim_seconds"] for r in rows), 3) if rows else 0.0,
        "mean_evals": round(statistics.fmean(r["n_evals"] for r in rows), 1) if rows else 0.0,
        "failures": [r["problem"] for r in rows if not r["reached"]],
        "errors": [{"problem": r["problem"], "error": r["error"]} for r in rows if not r.get("ok", True)],
        "per_problem": rows,
        "_traces": traces if keep_traces else None,
    }


def audit_solution(graph: dict, dev: str = "dev", frozen: str = "frozen",
                   label: str = "candidate") -> dict:
    """Score a solution on DEV and FROZEN, and report the generalization gap.

    Returns EVIDENCE, not a decision: an agent (or a human) reads `flags` and
    `verdict_inputs` to judge. The numbers themselves are not negotiable.
    """
    dev_suite, frozen_suite = get_suite(dev), get_suite(frozen)
    d = score_suite(graph, dev_suite)
    f = score_suite(graph, frozen_suite)

    gap_quality = round(d["mean_quality"] - f["mean_quality"], 6)
    gap_success = round(d["success_rate"] - f["success_rate"], 4)

    flags = []
    if gap_quality > OVERFIT_GAP:
        flags.append({
            "flag": "dev_score_unrepresentative", "severity": "high",
            "detail": (f"DEV 平均质量 {d['mean_quality']:.3f} 显著高于 FROZEN "
                       f"{f['mean_quality']:.3f}（间隙 {gap_quality:.3f} > 阈值 {OVERFIT_GAP}）"),
            "meaning": ("DEV 分数不能代表真实表现。两种成因需要区分："
                        "①方案被针对可见题目调过（过拟合）；②FROZEN 本身更难/地形更广。"
                        "若同一方案在历次审计中间隙持续扩大 → 更可能是①；"
                        "若所有方案的间隙都类似 → 更可能是②（题库难度差）。"),
        })
    if f["success_rate"] < MIN_FROZEN_SUCCESS:
        flags.append({
            "flag": "frozen_below_bar", "severity": "high",
            "detail": f"冻结题库达标率 {f['success_rate']:.0%} < 门槛 {MIN_FROZEN_SUCCESS:.0%}",
            "meaning": "不满足验收标准，无论 DEV 上多好",
        })
    if f["errors"]:
        flags.append({
            "flag": "runtime_errors", "severity": "high",
            "detail": f"冻结题库上有 {len(f['errors'])} 题运行报错",
            "meaning": "方案不稳健（可能对某些起点/场景不成立）",
            "errors": f["errors"],
        })
    if f["min_quality"] < 0.3 * f["mean_quality"]:
        flags.append({
            "flag": "inconsistent", "severity": "medium",
            "detail": f"冻结题库最差一题质量 {f['min_quality']:.3f} 远低于均值 {f['mean_quality']:.3f}",
            "meaning": "在某类地形上明显失效，非普适方案",
        })

    passed = (not any(x["severity"] == "high" for x in flags))
    return {
        "label": label,
        "dev": {k: v for k, v in d.items() if not k.startswith("_")},
        "frozen": {k: v for k, v in f.items() if not k.startswith("_")},
        "generalization_gap": {"quality": gap_quality, "success_rate": gap_success,
                               "threshold": OVERFIT_GAP},
        "flags": flags,
        "verdict_inputs": {
            "frozen_success_rate": f["success_rate"],
            "frozen_mean_quality": f["mean_quality"],
            "frozen_worst_problem_quality": f["min_quality"],
            "frozen_mean_sim_seconds": f["mean_sim_seconds"],
            "gap_quality": gap_quality,
            "passes_hard_checks": passed,
        },
        "acceptance": "PASS" if passed else "FAIL",
        "provenance": {"dev_fingerprint": d["fingerprint"],
                       "frozen_fingerprint": f["fingerprint"],
                       "min_frozen_success": MIN_FROZEN_SUCCESS,
                       "overfit_gap_threshold": OVERFIT_GAP},
        "note": ("打分为确定性计算，Agent 只可基于本报告判断，不可改动分数。"
                 "provenance 里的 fingerprint 可复核题库未被篡改。"),
    }


def compare_solutions(candidates: list[dict], dev: str = "dev",
                      frozen: str = "frozen") -> dict:
    """Audit several solutions and rank them by FROZEN performance (never by DEV).

    `candidates`: [{"label": str, "graph": {...}}, ...]
    """
    reports = [audit_solution(c["graph"], dev, frozen, c.get("label", f"cand{i}"))
               for i, c in enumerate(candidates)]
    ranked = sorted(
        reports,
        key=lambda r: (r["acceptance"] == "PASS",
                       r["frozen"]["success_rate"],
                       r["frozen"]["mean_quality"],
                       -r["frozen"]["mean_sim_seconds"]),
        reverse=True)
    return {
        "n_candidates": len(reports),
        "ranking": [{"label": r["label"], "acceptance": r["acceptance"],
                     "frozen_success": r["frozen"]["success_rate"],
                     "frozen_quality": r["frozen"]["mean_quality"],
                     "frozen_seconds": r["frozen"]["mean_sim_seconds"],
                     "gap": r["generalization_gap"]["quality"],
                     "flags": [f["flag"] for f in r["flags"]]} for r in ranked],
        "best": ranked[0]["label"] if ranked else None,
        "reports": ranked,
        "ranked_by": "FROZEN 表现（达标率→质量→耗时）；DEV 分数不参与排名，只用于算泛化间隙。",
    }
