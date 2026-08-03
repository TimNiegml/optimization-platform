"""FastAPI service — the backend a canvas (or any client) talks to.

    uvicorn optplat.api:app --reload           # or: python -m optplat.api

Endpoints:
  GET  /health              liveness
  GET  /catalog             algorithm palette + param schemas (for the canvas)
  GET  /backends            evaluator backends (bench sim / hardware / zemax / …)
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

from .backends import EvaluatorConfig, backend_catalog, build_evaluator
from .demo import demo_vocs
from .graph import GraphRunner
from .orchestrator import Orchestrator
from .registry import algorithm_catalog
from .vocs import VOCS

app = FastAPI(title="Optimization Platform API", version="0.1")

_WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


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


def _build_evaluator(vocs: VOCS, cfg: EvaluatorConfig):
    """Evaluator backends are plug-ins (see backends.py): function / hardware_sim /
    zemax / composite, plus anything a deployment registers."""
    return build_evaluator(vocs, cfg)


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
    return {"algorithms": algorithm_catalog(), "backends": backend_catalog()}


@app.get("/backends")
def backends():
    """Evaluator backends the canvas can offer (bench sim, hardware, Zemax…)."""
    return {"backends": backend_catalog()}


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
