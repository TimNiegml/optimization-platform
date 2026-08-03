"""Trace digest — turn a raw run trace into a diagnosis an LLM can actually read.

A run returns hundreds of raw evaluations (`history`) plus a stage log (`events`).
That is far too much for an LLM to reason over reliably, and eyeballing it invites
confident nonsense. This module compresses a trace into **numerical evidence**:

  * per-stage cost/benefit attribution — who spent the measurement seconds, and
    who actually improved the objective (finds "expensive but useless" stages)
  * failure-mode detectors — oscillation, stagnation, boundary sticking, fit
    rejection, wasted flat-region scanning
  * per-variable **length scale** (peak width) estimated from the trace, which is
    the physical quantity that should set step size / scan window
  * from that length scale, a **suggested search range** for the step-like params

Division of labour (deliberate): this module produces evidence and ranges; an LLM
reads it and decides which knob to change and what interval to search; AutoTuner
then searches that interval and the numbers are settled by scoring, not by the
LLM guessing a value.

IMPORTANT: one trace is an anecdote. Noise makes single runs lie. Use
`digest_many()` over several seeds/trials and trust only patterns that repeat —
`confidence` reports in how many runs each finding appeared.
"""
from __future__ import annotations

import re
import statistics
from typing import Iterable, Optional

from .vocs import VOCS


def _stage_base(s) -> str:
    return str(s if s is not None else "").split(":")[0]


def _finite(vals: Iterable) -> list[float]:
    out = []
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and abs(f) != float("inf"):
            out.append(f)
    return out


# ---------------------------------------------------------------- length scale
def length_scales(history: list[dict], vocs: VOCS, objective: str) -> dict:
    """Estimate, per variable, the scale over which `objective` changes — i.e. the
    peak width seen in this trace.

    Method: take the points in the top half of the observed objective range and
    measure their spread along each variable. That spread is the width of the
    'good' region: a step much larger than it overshoots the peak; a step much
    smaller than it wastes evaluations. Returned both in absolute units and as a
    fraction of the variable's full range (which is what `step_frac`-style
    parameters are expressed in).
    """
    ys = _finite(r.get(objective) for r in history if objective in r)
    out: dict[str, dict] = {}
    if len(ys) < 4:
        return out
    ymin, ymax = min(ys), max(ys)
    if ymax - ymin <= 0:
        return out
    half = ymin + 0.5 * (ymax - ymin)                 # half-max threshold
    good = [r for r in history
            if objective in r and _finite([r[objective]]) and float(r[objective]) >= half]
    if len(good) < 2:
        return out
    for name, var in vocs.variables.items():
        xs = _finite(r.get(name) for r in good if name in r)
        if len(xs) < 2:
            continue
        width = max(xs) - min(xs)
        rng = var.high - var.low
        if rng <= 0:
            continue
        frac = width / rng
        out[name] = {
            "peak_width": round(width, 6),
            "peak_width_frac": round(frac, 6),
            # a few samples across the good region: step ≈ width/4 is a sane centre
            "suggested_step": round(max(width / 4.0, 1e-9), 6),
            "suggested_step_frac": round(max(frac / 4.0, 1e-6), 6),
            "range": [var.low, var.high],
        }
    return out


def suggested_search_space(scales: dict) -> dict:
    """Turn length scales into a **search interval** for step-like parameters.

    Centre the interval on width/4 and span a decade around it (÷4 … ×4, clipped
    to (0, 1]). This is the object the LLM is meant to sanity-check and hand to
    AutoTuner — it replaces blind searching of the schema's full min/max extremes.
    """
    if not scales:
        return {}
    fracs = [s["suggested_step_frac"] for s in scales.values()]
    centre = statistics.fmean(fracs)
    low = max(1e-3, centre / 4.0)
    high = min(1.0, max(centre * 4.0, low * 2))
    span = {"low": round(low, 5), "high": round(high, 5), "n": 4}
    # window-style params want to cover the peak, so they sit wider than a step
    win_low = min(1.0, max(2e-3, centre * 2))
    win_high = min(1.0, max(win_low * 2, centre * 12))
    window = {"low": round(win_low, 5), "high": round(win_high, 5), "n": 4}
    return {
        "step_frac": span,
        "init_step_frac": span,
        "probe_frac": {"low": round(max(1e-3, low / 4), 5), "high": round(high / 2, 5), "n": 3},
        "span_frac": window,
        "per_axis_suggested_step": {k: v["suggested_step"] for k, v in scales.items()},
    }


# ---------------------------------------------------------------- detectors
def _stage_rows(history: list[dict]) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    for i, r in enumerate(history):
        idx.setdefault(_stage_base(r.get("stage")), []).append(i)
    return idx


def stage_attribution(history: list[dict], objective: str,
                      costs: Optional[dict] = None) -> list[dict]:
    """Per stage: how many evaluations, how much objective improvement, and what
    share of the measurement time — exposes stages that cost a lot and buy little."""
    costs = costs or {}
    rows = _stage_rows(history)
    best_so_far = float("-inf")
    per_stage: dict[str, dict] = {}
    for i, r in enumerate(history):
        st = _stage_base(r.get("stage"))
        d = per_stage.setdefault(st, {"stage": st, "n_evals": 0, "gain": 0.0,
                                      "best": float("-inf")})
        d["n_evals"] += 1
        y = r.get(objective)
        try:
            y = float(y)
        except (TypeError, ValueError):
            continue
        if y > d["best"]:
            d["best"] = y
        if y > best_so_far:
            d["gain"] += (y - best_so_far) if best_so_far > float("-inf") else 0.0
            best_so_far = y
    raw_gain = sum(max(0.0, d["gain"]) for d in per_stage.values())
    total_gain = raw_gain or 1.0
    total_evals = sum(d["n_evals"] for d in per_stage.values()) or 1
    # If NOTHING improved run-wide (e.g. the start point was already optimal), it
    # is wrong to blame individual stages — say so instead of flagging them all.
    no_progress = raw_gain <= 1e-12
    out = []
    for st, d in per_stage.items():
        n = d["n_evals"]
        share = n / total_evals
        gshare = max(0.0, d["gain"]) / total_gain
        if no_progress:
            verdict = "全局无改善（起点可能已是最优/该目标不敏感），不单独归咎本阶段"
        elif share > 0.25 and gshare < 0.05:
            verdict = "低效：占用评估多、增益少"
        else:
            verdict = "正常"
        out.append({
            "stage": st,
            "n_evals": n,
            "eval_share": round(share, 4),
            "objective_gain": round(max(0.0, d["gain"]), 6),
            "gain_share": round(gshare, 4) if not no_progress else 0.0,
            "gain_per_eval": round(max(0.0, d["gain"]) / n, 8) if n else 0.0,
            "best_seen": None if d["best"] == float("-inf") else round(d["best"], 6),
            "verdict": verdict,
        })
    out.sort(key=lambda r: -r["eval_share"])
    return out


def detect_issues(history: list[dict], events: list[str], vocs: VOCS,
                  objective: str, state: Optional[dict] = None) -> list[dict]:
    """Failure-mode detectors. Each finding carries the evidence that triggered it
    so a reader (human or LLM) can check the claim rather than trust it."""
    findings: list[dict] = []
    ys = [(_finite([r.get(objective)]) or [None])[0] for r in history]
    ys = [y for y in ys if y is not None]
    n = len(ys)

    # --- stagnation: long tail with no improvement over the running best ---
    if n >= 12:
        tail = max(6, n // 3)
        best_before = max(ys[: n - tail]) if n - tail > 0 else float("-inf")
        improved = sum(1 for y in ys[n - tail:] if y > best_before + 1e-12)
        if improved == 0:
            findings.append({
                "issue": "stagnation", "severity": "high",
                "detail": f"最后 {tail} 次评估（占 {tail/n:.0%}）未超过此前最优值，属于纯浪费",
                "evidence": {"tail_evals": tail, "total_evals": n,
                             "best_before_tail": round(best_before, 6)},
                "suggests": "缩短该阶段 max_iter / 加 stop.target 提前结束",
            })

    # --- oscillation: objective repeatedly reverses near the end ---
    if n >= 10:
        tail = ys[-min(20, n):]
        deltas = [b - a for a, b in zip(tail, tail[1:])]
        scale = (max(tail) - min(tail)) or 1.0
        sig = [d for d in deltas if abs(d) > 0.02 * scale]
        flips = sum(1 for a, b in zip(sig, sig[1:]) if a * b < 0)
        if len(sig) >= 4 and flips >= max(3, int(0.6 * len(sig))):
            findings.append({
                "issue": "oscillation", "severity": "medium",
                "detail": f"末段目标值反复升降（{flips} 次反向），典型步长过大越过峰顶",
                "evidence": {"reversals": flips, "significant_moves": len(sig)},
                "suggests": "减小 step_frac / init_step_frac，或缩小其搜索区间上界",
            })

    # --- boundary sticking: a variable ends pinned at its limit ---
    st = state or (history[-1] if history else {})
    for name, var in vocs.variables.items():
        if name not in st:
            continue
        v = (_finite([st[name]]) or [None])[0]
        if v is None:
            continue
        rng = var.high - var.low
        if rng <= 0:
            continue
        if abs(v - var.low) < 1e-3 * rng or abs(v - var.high) < 1e-3 * rng:
            findings.append({
                "issue": "boundary_stick", "severity": "high",
                "detail": f"{name} 收敛在量程边界 {v:.4g}（范围 {var.low}~{var.high}）",
                "evidence": {"variable": name, "value": round(v, 6)},
                "suggests": "量程可能设窄了，或起点离真解太远/方向错误",
            })

    # --- flat region: a stage sampled where nothing changes ---
    for st_name, idxs in _stage_rows(history).items():
        if st_name in ("init", "") or len(idxs) < 6:
            continue
        vals = _finite(history[i].get(objective) for i in idxs)
        if len(vals) < 6:
            continue
        spread = max(vals) - min(vals)
        gspread = (max(ys) - min(ys)) if ys else 0.0
        if gspread > 0 and spread < 0.02 * gspread:
            findings.append({
                "issue": "flat_scan", "severity": "medium",
                "detail": f"阶段『{st_name}』{len(idxs)} 次评估目标几乎不变（跨度仅占全局 {spread/gspread:.1%}）",
                "evidence": {"stage": st_name, "n_evals": len(idxs)},
                "suggests": "该区域无信息：换更粗的扫描或改用相对窗口 span_frac 贴近起点",
            })

    # --- fit rejections read off the event log ---
    rejects = [e for e in (events or []) if "R²" in e or "fit rejected" in e or "回退" in e]
    if rejects:
        findings.append({
            "issue": "fit_rejected", "severity": "medium",
            "detail": f"拟合被 R² 守门拒绝 {len(rejects)} 次并回退",
            "evidence": {"count": len(rejects), "sample": rejects[:2]},
            "suggests": "采样点数不足或窗口不含峰：增大 n_samples，或用 span_frac 把采样窗口对准起点附近",
        })
    return findings


# ---------------------------------------------------------------- entry points
def digest(result: dict, vocs: VOCS, objective: Optional[str] = None) -> dict:
    """Compress ONE run into a diagnosis + suggested search space.

    `result` is what GraphRunner/Orchestrator return (history/events/state/...).
    """
    history = result.get("history") or []
    events = result.get("events") or []
    obj = objective or (next(iter(vocs.objectives)) if vocs.objectives else "y1")
    scales = length_scales(history, vocs, obj)
    return {
        "objective": obj,
        "n_evals": result.get("n_evals", len(history)),
        "sim_seconds": result.get("sim_seconds", 0.0),
        "final_objectives": result.get("objectives", {}),
        "stage_attribution": stage_attribution(history, obj),
        "issues": detect_issues(history, events, vocs, obj, result.get("state")),
        "length_scales": scales,
        "suggested_search_space": suggested_search_space(scales),
        "fits": result.get("fits", {}),
        "caveat": "单条 trace 只是轶事；带噪场景请用 digest_many() 跨种子确认后再改算法。",
    }


def digest_many(results: list[dict], vocs: VOCS, objective: Optional[str] = None) -> dict:
    """Digest SEVERAL runs and keep only what repeats — the anti-anecdote guard.

    Returns each issue with `confidence` = fraction of runs it appeared in, and
    length scales averaged across runs. A finding seen in 1 of 5 runs is noise;
    one seen in 5 of 5 is a real defect.
    """
    if not results:
        return {"n_runs": 0, "issues": [], "suggested_search_space": {}}
    obj = objective or (next(iter(vocs.objectives)) if vocs.objectives else "y1")
    singles = [digest(r, vocs, obj) for r in results]
    n = len(singles)

    counts: dict[str, dict] = {}
    for d in singles:
        seen = set()
        for f in d["issues"]:
            key = f["issue"] + "|" + str(f.get("evidence", {}).get("variable", ""))
            if key in seen:
                continue
            seen.add(key)
            c = counts.setdefault(key, {"finding": f, "runs": 0})
            c["runs"] += 1
    issues = []
    for c in counts.values():
        f = dict(c["finding"])
        f["confidence"] = round(c["runs"] / n, 3)
        f["seen_in_runs"] = f"{c['runs']}/{n}"
        f["reliable"] = c["runs"] >= max(2, (n + 1) // 2)   # majority of runs
        issues.append(f)
    issues.sort(key=lambda f: (-f["confidence"], f["issue"]))

    merged_scales: dict[str, dict] = {}
    for name in {k for d in singles for k in d["length_scales"]}:
        vals = [d["length_scales"][name] for d in singles if name in d["length_scales"]]
        if not vals:
            continue
        merged_scales[name] = {
            "peak_width": round(statistics.fmean(v["peak_width"] for v in vals), 6),
            "peak_width_frac": round(statistics.fmean(v["peak_width_frac"] for v in vals), 6),
            "suggested_step": round(statistics.fmean(v["suggested_step"] for v in vals), 6),
            "suggested_step_frac": round(statistics.fmean(v["suggested_step_frac"] for v in vals), 6),
            "range": vals[0]["range"],
            "n_runs": len(vals),
        }
    return {
        "n_runs": n,
        "objective": obj,
        "mean_evals": round(statistics.fmean(d["n_evals"] for d in singles), 2),
        "mean_sim_seconds": round(statistics.fmean(d["sim_seconds"] or 0.0 for d in singles), 2),
        "issues": issues,
        "reliable_issues": [f for f in issues if f["reliable"]],
        "length_scales": merged_scales,
        "suggested_search_space": suggested_search_space(merged_scales),
        "note": "只有 reliable=true（多数运行都出现）的问题才值得据此改算法。",
    }
