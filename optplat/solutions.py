"""Solution store — persist & retrieve whole workflows ("方案").

A *solution* is a saved optimization workflow: a graph IR ({nodes, edges,
until?}) plus the simulation scenario (bench) and evaluator settings it was
built for. This is what backs the agent's "加载某个以前的解决方案" ability and the
canvas's 保存/加载/方案库 buttons.

Two sources, one interface:
  * BUILTIN presets — one tailored workflow per simulated scenario (mirrors the
    canvas `载入示例（按场景）` presets), always available.
  * saved solutions — JSON files under a solutions directory
    (``OPTPLAT_SOLUTIONS_DIR`` or ``<repo>/solutions``), so an agent (or a user)
    can save a workflow and reload it in a later session.

Pure standard-library (json/os) — no new dependency.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional


# ---------------- graph helpers ----------------
def linear_graph(steps: list[dict], until: Optional[str] = None) -> dict:
    """Build a start → n1 → n2 → … → end graph from a list of stage dicts.

    Each step: {"algorithm", "variables", "objective", params…, optional
    "keep"/"stop_target"/"stage"}. Returns a runnable {nodes, edges, until?}.
    """
    nodes: list[dict] = [{"id": "start", "type": "start"}]
    edges: list[dict] = []
    prev = "start"
    for i, step in enumerate(steps, 1):
        nid = f"n{i}"
        data = {k: v for k, v in step.items() if k not in ("stop_target",)}
        if step.get("stop_target"):
            data.setdefault("stop", {})["target"] = step["stop_target"]
            data.pop("stop_target", None)
        nodes.append({"id": nid, "type": "algorithm", "data": data})
        edges.append({"source": prev, "target": nid})
        prev = nid
    nodes.append({"id": "end", "type": "end"})
    edges.append({"source": prev, "target": "end"})
    graph: dict = {"nodes": nodes, "edges": edges}
    if until:
        graph["until"] = until
    return graph


# ---------------- built-in presets (per simulated scenario) ----------------
# Mirrors the canvas `载入示例（按场景）` presets so an agent gets the same tailored
# starting workflow the UI offers. Keyed by preset name.
_BUILTIN_DEFS = {
    "single_peak_default": {
        "bench": "single_peak", "description": "单峰：网格找光 → 单纯形精调 → 公式法均衡 y2",
        "steps": [
            {"algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
             "stop_target": "y1>0.2"},
            {"algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1"},
            {"algorithm": "formula", "variables": ["x3"], "objective": "y2", "keep": "y1>0.8"},
        ],
        "until": "y1>=0.98 and y2>=0.95",
    },
    "multi_peak_bayes": {
        "bench": "multi_peak", "description": "多峰：贝叶斯全局找主瓣 → 单纯形精调 → 高斯拟合 y2",
        "steps": [
            {"algorithm": "bayesian", "variables": ["x1", "x2"], "objective": "y1", "n_calls": 60},
            {"algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1"},
            {"algorithm": "gaussian_fit", "variables": ["x3"], "objective": "y2", "keep": "y1>0.8"},
        ],
        "until": "y1>=0.95 and y2>=0.9",
    },
    "skew_gradient": {
        "bench": "skew_peak", "description": "偏斜相关峰：粗找 → 梯度上升沿脊对准 → 公式法 y2",
        "steps": [
            {"algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
             "stop_target": "y1>0.2"},
            {"algorithm": "gradient_ascent", "variables": ["x1", "x2"], "objective": "y1"},
            {"algorithm": "formula", "variables": ["x3"], "objective": "y2", "keep": "y1>0.8"},
        ],
        "until": "y1>=0.97 and y2>=0.92",
    },
    "rosenbrock_simplex": {
        "bench": "rosenbrock", "description": "香蕉谷：密网格入谷 → 单纯形沿弯谷 → 公式法 y2",
        "steps": [
            {"algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
             "n_per_axis": 11, "stop_target": "y1>0.1"},
            {"algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1"},
            {"algorithm": "formula", "variables": ["x3"], "objective": "y2", "keep": "y1>0.6"},
        ],
        "until": "y1>=0.9 and y2>=0.8",
    },
    "rastrigin_bayes": {
        "bench": "rastrigin", "description": "强多峰：贝叶斯全局 → 坐标下降落盆地 → 公式法 y2",
        "steps": [
            {"algorithm": "bayesian", "variables": ["x1", "x2"], "objective": "y1", "n_calls": 80},
            {"algorithm": "coordinate_descent", "variables": ["x1", "x2"], "objective": "y1"},
            {"algorithm": "formula", "variables": ["x3"], "objective": "y2", "keep": "y1>0.6"},
        ],
        "until": "y1>=0.9 and y2>=0.8",
    },
    "ackley_bayes": {
        "bench": "ackley", "description": "多峰+外围平坦：贝叶斯找中心 → 梯度上升登尖峰 → 高斯拟合 y2",
        "steps": [
            {"algorithm": "bayesian", "variables": ["x1", "x2"], "objective": "y1", "n_calls": 70},
            {"algorithm": "gradient_ascent", "variables": ["x1", "x2"], "objective": "y1"},
            {"algorithm": "gaussian_fit", "variables": ["x3"], "objective": "y2", "keep": "y1>0.6"},
        ],
        "until": "y1>=0.9 and y2>=0.8",
    },
}


def _builtin(name: str) -> dict:
    d = _BUILTIN_DEFS[name]
    return {
        "name": name,
        "source": "builtin",
        "bench": d["bench"],
        "description": d["description"],
        "graph": linear_graph(d["steps"], d.get("until")),
    }


def builtin_solutions() -> list[dict]:
    return [_builtin(n) for n in _BUILTIN_DEFS]


# ---------------- file-backed saved solutions ----------------
def solutions_dir() -> str:
    d = os.environ.get("OPTPLAT_SOLUTIONS_DIR")
    if not d:
        d = os.path.join(os.path.dirname(os.path.dirname(__file__)), "solutions")
    return d


def _safe_name(name: str) -> str:
    keep = "-_. "
    cleaned = "".join(c for c in name if c.isalnum() or c in keep or ord(c) > 127).strip()
    cleaned = cleaned.replace(" ", "_")
    if not cleaned:
        raise ValueError("empty solution name")
    return cleaned


def _saved_path(name: str) -> str:
    return os.path.join(solutions_dir(), _safe_name(name) + ".json")


def save_solution(name: str, graph: dict, bench: str = "single_peak",
                  description: str = "", evaluator: Optional[dict] = None) -> dict:
    """Persist a workflow so it can be reloaded later. Overwrites same name."""
    os.makedirs(solutions_dir(), exist_ok=True)
    rec = {
        "name": name,
        "source": "saved",
        "bench": bench,
        "description": description,
        "graph": graph,
        "evaluator": evaluator or {},
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(_saved_path(name), "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=2)
    return rec


def saved_solutions() -> list[dict]:
    d = solutions_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as fh:
                rec = json.load(fh)
            rec.setdefault("name", fn[:-5])
            rec["source"] = "saved"
            out.append(rec)
        except Exception:
            continue
    return out


def list_solutions() -> list[dict]:
    """All solutions (built-in presets + saved), each with a lightweight summary."""
    out = []
    for rec in builtin_solutions() + saved_solutions():
        g = rec.get("graph", {})
        algos = [n.get("data", {}).get("algorithm")
                 for n in g.get("nodes", []) if n.get("type") == "algorithm"]
        out.append({
            "name": rec["name"],
            "source": rec["source"],
            "bench": rec.get("bench"),
            "description": rec.get("description", ""),
            "steps": [a for a in algos if a],
            "until": g.get("until"),
        })
    return out


def get_solution(name: str) -> dict:
    """Return the full solution record (incl. graph) for `name`, or raise."""
    if name in _BUILTIN_DEFS:
        return _builtin(name)
    path = _saved_path(name)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["source"] = "saved"
        return rec
    known = list(_BUILTIN_DEFS) + [r["name"] for r in saved_solutions()]
    raise KeyError(f"unknown solution: {name} (known: {known})")


def delete_solution(name: str) -> bool:
    """Delete a saved solution file. Built-ins can't be deleted. Returns True if removed."""
    if name in _BUILTIN_DEFS:
        raise ValueError(f"'{name}' is a built-in preset and cannot be deleted")
    path = _saved_path(name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
