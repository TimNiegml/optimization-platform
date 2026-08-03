"""FastAPI service — the backend a canvas (or any client) talks to.

    uvicorn optplat.api:app --reload           # or: python -m optplat.api

Endpoints:
  GET  /health              liveness
  GET  /catalog             algorithm palette + param schemas (for the canvas)
  GET  /backends            evaluator backends (bench sim / hardware / zemax / …)
  GET  /vocs                default demo VOCS (variables / objectives)
  POST /zemax/inspect       read a .zmx -> surfaces / pickable variables / MFE rows
  POST /zemax/binding       the user's picks -> binding + VOCS JSON
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


class InspectRequest(BaseModel):
    """Open a design in OpticStudio and report what could become a variable."""
    connection: dict                       # ZemaxConnection fields
    surfaces: Optional[list[int]] = None   # limit to these surface numbers
    include_thickness: bool = True
    include_order: bool = False


class BindingRequest(InspectRequest):
    selections: list[dict]                 # what the user picked in the UI
    merit: Optional[dict] = None


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


def _with_zemax_session(connection: dict, fn):
    """Open a short-lived OpticStudio session for one inspection request."""
    from .zemax import ZemaxBinding, ZemaxConnection, ZemaxEvaluator
    conn = ZemaxConnection(**connection)
    ev = ZemaxEvaluator(ZemaxBinding(), conn)
    try:
        ev.bind()
        return fn(ev.client)
    finally:
        ev.close()


@app.post("/zemax/inspect")
def zemax_inspect(req: InspectRequest):
    """Read the open/opened .zmx: surfaces, pickable variables, MFE rows.

    This is what lets a user *pick* `S3 · align in · Tilt About X` instead of
    knowing that it is surface 3, PARM 3. Followers already authored in the
    design (pickup solves) come back attached to the choice they follow.
    """
    from .zemax_inspect import inspect_system, knob_choices, operand_choices
    try:
        def work(client):
            snap = inspect_system(client, surfaces=req.surfaces)
            choices = knob_choices(snap, include_thickness=req.include_thickness,
                                   include_order=req.include_order)
            return {
                "system": snap.model_dump(exclude={"operands"}),
                "choices": [{**c.model_dump(), "id": c.id()} for c in choices],
                "operands": operand_choices(snap),
            }
        return _with_zemax_session(req.connection, work)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.post("/zemax/binding")
def zemax_binding(req: BindingRequest):
    """Turn the user's picks into the binding JSON the `zemax` backend consumes."""
    from .zemax import MeritSpec
    from .zemax_inspect import build_binding, inspect_system
    try:
        def work(client):
            snap = inspect_system(client, surfaces=req.surfaces)
            binding = build_binding(snap, req.selections,
                                    MeritSpec(**req.merit) if req.merit else None)
            return {"binding": binding.model_dump(),
                    "vocs": binding.build_vocs().model_dump()}
        return _with_zemax_session(req.connection, work)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


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
