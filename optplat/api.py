"""FastAPI service — the backend a canvas (or any client) talks to.

    uvicorn optplat.api:app --reload           # or: python -m optplat.api

Endpoints:
  GET  /health              liveness
  GET  /catalog             algorithm palette + param schemas (for the canvas)
  GET  /vocs                default demo VOCS (variables / objectives)
  POST /run/graph           run a {nodes, edges} graph JSON  -> result
  POST /run/pipeline        run a block pipeline JSON         -> result
  GET  /                    the drag-drop canvas (static web/index.html)

Evaluator: the simulated optical bench (function or noisy hardware sim). A real
deployment registers its own evaluator the same way algorithms are registered.
FastAPI (MIT) + uvicorn (BSD) — permissive.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .demo import BENCHES, bench_func, demo_vocs, optical_bench
from .evaluator import Evaluator
from .graph import GraphRunner
from .hardware import HardwareEvaluator, SafetyLimits, SimulatedMeter, SimulatedStage
from .orchestrator import Orchestrator
from .registry import algorithm_catalog
from .vocs import VOCS

app = FastAPI(title="Optimization Platform API", version="0.1")

_WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


class EvaluatorConfig(BaseModel):
    mode: str = "function"          # "function" | "hardware_sim"
    bench: str = "single_peak"      # which simulated bench (see /benches)
    noise: float = 0.0
    averages: int = 1
    safety: bool = False
    # optional per-variable safe range override: {"x1": [low, high], ...}
    safety_limits: Optional[dict[str, list[float]]] = None


class RunRequest(BaseModel):
    vocs: Optional[dict] = None
    evaluator: EvaluatorConfig = EvaluatorConfig()
    start_point: Optional[dict[str, float]] = None
    eval_budget: int = 5000


class RunGraphRequest(RunRequest):
    graph: dict


class RunPipelineRequest(RunRequest):
    pipeline: dict


def _build_vocs(v: Optional[dict]) -> VOCS:
    return VOCS(**v) if v else demo_vocs()


def _safety_limits(vocs: VOCS, cfg: EvaluatorConfig) -> Optional[SafetyLimits]:
    if not cfg.safety:
        return None
    if cfg.safety_limits:
        limits = {n: (float(lohi[0]), float(lohi[1]))
                  for n, lohi in cfg.safety_limits.items() if len(lohi) == 2}
    else:
        limits = {n: (v.low, v.high) for n, v in vocs.variables.items()}
    return SafetyLimits(limits)


def _build_evaluator(vocs: VOCS, cfg: EvaluatorConfig):
    func = bench_func(cfg.bench)
    if cfg.mode == "hardware_sim":
        stage = SimulatedStage(vocs.initial_point())
        meter = SimulatedMeter(stage, func, noise=cfg.noise, seed=0)
        return HardwareEvaluator(stage, meter, averages=cfg.averages,
                                 safety=_safety_limits(vocs, cfg))
    return Evaluator(func)


def _result(res: dict) -> dict[str, Any]:
    # history can be large; keep it but let the client decide what to plot
    return {
        "state": res["state"],
        "objectives": res["objectives"],
        "n_evals": res["n_evals"],
        "events": res["events"],
        "history": res["history"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/catalog")
def catalog():
    return {"algorithms": algorithm_catalog()}


@app.get("/benches")
def benches():
    """Selectable simulated evaluation scenarios for the canvas 场景 dropdown."""
    return {"benches": [{"name": n, "label": b["label"], "desc": b["desc"]}
                        for n, b in BENCHES.items()]}


@app.get("/vocs")
def vocs():
    v = demo_vocs()
    return {
        "variables": {n: {"low": var.low, "high": var.high} for n, var in v.variables.items()},
        "objectives": {n: {"mode": o.mode.value} for n, o in v.objectives.items()},
    }


@app.post("/run/graph")
def run_graph(req: RunGraphRequest):
    try:
        vocs = _build_vocs(req.vocs)
        ev = _build_evaluator(vocs, req.evaluator)
        res = GraphRunner(vocs, ev, req.graph, eval_budget=req.eval_budget,
                          start_point=req.start_point).run()
        return _result(res)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.post("/run/pipeline")
def run_pipeline(req: RunPipelineRequest):
    try:
        vocs = _build_vocs(req.vocs)
        ev = _build_evaluator(vocs, req.evaluator)
        res = Orchestrator(vocs, ev, req.pipeline, eval_budget=req.eval_budget,
                           start_point=req.start_point).run()
        return _result(res)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.get("/")
def index():
    path = os.path.join(_WEB, "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="canvas not built")


def main():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
