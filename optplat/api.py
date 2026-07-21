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

import asyncio
import contextlib
import json
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .autotune import TuneSpec, run_autotune
from .demo import BENCHES, bench_func, demo_vocs, optical_bench
from .evaluator import Evaluator
from .graph import GraphRunner
from .hardware import HardwareEvaluator, SafetyLimits, SimulatedMeter, SimulatedStage
from .mcp_server import mcp
from .orchestrator import Orchestrator
from .registry import REGISTRY, algorithm_catalog
from .userdev import load_device
from .vocs import VOCS
from .workspace import WORKSPACE

# Optional EXTERNAL device: point the server at a user module that declares its
# own axes (x) + meters (y) and the platform drives THOSE instead of the demo
# bench — the canvas / REST / MCP then reflect the real x/y automatically.
#   OPTPLAT_DEVICE=path/to/device.py python -m optplat.api
DEVICE = None
_dev_path = os.environ.get("OPTPLAT_DEVICE")
if _dev_path:
    DEVICE = load_device(_dev_path)

# The MCP server is mounted at /mcp (streamable-HTTP) so ONE process serves the
# canvas, the REST API AND the agent-facing MCP endpoint, all sharing the same
# in-memory WORKSPACE — that shared state is what lets an agent's edits show up
# on the canvas automatically. Its session manager must run for the lifetime of
# the app, so we drive it from the FastAPI lifespan.
mcp.settings.streamable_http_path = "/"

# Optional bearer token guarding /mcp and /workspace (set OPTPLAT_TOKEN in prod;
# unset = open, for localhost dev). EventSource can't send headers, so the SSE
# stream also accepts the token as a ?token= query param.
_TOKEN = os.environ.get("OPTPLAT_TOKEN")


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Optimization Platform API", version="0.2", lifespan=_lifespan)


@app.middleware("http")
async def _auth(request: Request, call_next):
    path = request.url.path
    if _TOKEN and (path.startswith("/mcp") or path.startswith("/workspace")):
        auth = request.headers.get("authorization", "")
        provided = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        provided = provided or request.query_params.get("token")
        if provided != _TOKEN:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


_WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "web")


class EvaluatorConfig(BaseModel):
    mode: str = "function"          # "function" | "hardware_sim"
    bench: str = "single_peak"      # which simulated bench (see /benches)
    noise: float = 0.0
    averages: int = 1
    safety: bool = False
    # optional per-variable safe range override: {"x1": [low, high], ...}
    safety_limits: Optional[dict[str, list[float]]] = None
    # optional per-channel read-cost override (seconds): {"y1": 1.0, "y2": 2.0}
    channel_costs: Optional[dict[str, float]] = None
    # optional per-channel measurement group: {"y1": "g1", "y2": "g1", "y3": "g2"}.
    # Same group = measured in parallel (time = max); different groups = serial (sum).
    channel_groups: Optional[dict[str, str]] = None


class RunRequest(BaseModel):
    vocs: Optional[dict] = None
    evaluator: EvaluatorConfig = EvaluatorConfig()
    start_point: Optional[dict[str, float]] = None
    eval_budget: int = 5000


class RunGraphRequest(RunRequest):
    graph: dict


class RunPipelineRequest(RunRequest):
    pipeline: dict


def _build_vocs(v: Optional[dict], cfg: Optional[EvaluatorConfig] = None) -> VOCS:
    if v:
        vocs = VOCS(**v)
    elif DEVICE is not None:
        vocs = DEVICE.vocs()
    else:
        vocs = demo_vocs()
    if cfg and cfg.channel_costs:                 # per-channel read-cost override
        for name, cost in cfg.channel_costs.items():
            if name in vocs.objectives:
                vocs.objectives[name].cost = float(cost)
    return vocs


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
    if DEVICE is not None:                        # user's real/simulated instruments
        return DEVICE.evaluator(averages=cfg.averages, safety=_safety_limits(vocs, cfg))
    func = bench_func(cfg.bench)
    costs = {n: o.cost for n, o in vocs.objectives.items()}
    groups = {n: g for n, g in (cfg.channel_groups or {}).items() if g not in (None, "")}
    if cfg.mode == "hardware_sim":
        stage = SimulatedStage(vocs.initial_point())
        meter = SimulatedMeter(stage, func, noise=cfg.noise, seed=0)
        return HardwareEvaluator(stage, meter, averages=cfg.averages,
                                 safety=_safety_limits(vocs, cfg), costs=costs,
                                 groups=groups)
    return Evaluator(func, costs=costs, groups=groups)


def _result(res: dict) -> dict[str, Any]:
    # history can be large; keep it but let the client decide what to plot
    return {
        "state": res["state"],
        "objectives": res["objectives"],
        "n_evals": res["n_evals"],
        "events": res["events"],
        "history": res["history"],
        "fits": res.get("fits", {}),
        "reads": res.get("reads", {}),
        "sim_seconds": res.get("sim_seconds", 0.0),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/catalog")
def catalog():
    return {"algorithms": algorithm_catalog()}


@app.get("/benches")
def benches():
    """Selectable simulated evaluation scenarios for the canvas 场景 dropdown.
    When an external device is loaded, there is no swappable bench — report the
    single device scenario so the UI shows what it is driving."""
    if DEVICE is not None:
        info = DEVICE.info()
        return {"benches": [{"name": "device",
                             "label": f"外部设备（{info['n_axes']}×x / {info['n_meters']}×y）",
                             "desc": f"来自 {info['source']}；起点=轴当前位置。",
                             "smooth": False}], "device": True}
    return {"benches": [{"name": n, "label": b["label"], "desc": b["desc"],
                         "smooth": b.get("smooth", False)}
                        for n, b in BENCHES.items()]}


class SurfaceRequest(BaseModel):
    bench: str = "single_peak"
    objective: str = "y1"
    xvar: str
    yvar: str
    fixed: dict[str, float] = {}          # values for the other (held) variables
    nx: int = 41
    ny: int = 41
    xrange: Optional[list[float]] = None  # [low, high]; defaults to VOCS bounds
    yrange: Optional[list[float]] = None


@app.post("/surface")
def surface(req: SurfaceRequest):
    """Sample a bench's response surface on a 2-D grid over (xvar, yvar), holding
    the other variables fixed — so the canvas can draw a true response-surface
    contour for the continuous (纯函数) benches."""
    if DEVICE is not None:                         # a real device has no analytic surface
        raise HTTPException(status_code=400,
                            detail="external device has no analytic response surface")
    try:
        v = demo_vocs()
        func = bench_func(req.bench)
        xv, yv = v.variables[req.xvar], v.variables[req.yvar]
        xlo, xhi = req.xrange or [xv.low, xv.high]
        ylo, yhi = req.yrange or [yv.low, yv.high]
        nx, ny = max(2, min(req.nx, 121)), max(2, min(req.ny, 121))
        base = v.initial_point(); base.update(req.fixed or {})
        xs = [xlo + (xhi - xlo) * i / (nx - 1) for i in range(nx)]
        ys = [ylo + (yhi - ylo) * j / (ny - 1) for j in range(ny)]
        z = []
        for yj in ys:
            row = []
            for xi in xs:
                p = dict(base); p[req.xvar] = xi; p[req.yvar] = yj
                row.append(func(p)[req.objective])
            z.append(row)
        return {"xs": xs, "ys": ys, "z": z, "objective": req.objective}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.get("/vocs")
def vocs():
    v = DEVICE.vocs() if DEVICE is not None else demo_vocs()
    return {
        "variables": {n: {"low": var.low, "high": var.high} for n, var in v.variables.items()},
        "objectives": {n: {"mode": o.mode.value, "cost": o.cost,
                           "device": o.device, "param": o.param, "group": o.group}
                       for n, o in v.objectives.items()},
    }


def _start_point(req_start: Optional[dict]) -> Optional[dict]:
    # explicit request start wins; else for a real device use its CURRENT axis
    # positions (no absolute origin — start from where the hardware already is).
    if req_start:
        return req_start
    return DEVICE.current_point() if DEVICE is not None else None


@app.post("/run/graph")
def run_graph(req: RunGraphRequest):
    try:
        vocs = _build_vocs(req.vocs, req.evaluator)
        ev = _build_evaluator(vocs, req.evaluator)
        res = GraphRunner(vocs, ev, req.graph, eval_budget=req.eval_budget,
                          start_point=_start_point(req.start_point)).run()
        return _result(res)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.post("/run/pipeline")
def run_pipeline(req: RunPipelineRequest):
    try:
        vocs = _build_vocs(req.vocs, req.evaluator)
        ev = _build_evaluator(vocs, req.evaluator)
        res = Orchestrator(vocs, ev, req.pipeline, eval_budget=req.eval_budget,
                           start_point=_start_point(req.start_point)).run()
        return _result(res)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


@app.get("/device")
def device_info():
    """External-device info (n axes / n meters / current pose) when the server was
    started with OPTPLAT_DEVICE, else {device: None}. The canvas uses /vocs to
    auto-build the right number of x/y; this endpoint is for a status badge."""
    return {"device": DEVICE.info() if DEVICE is not None else None}


@app.get("/autotune/space")
def autotune_space():
    """Per-phase algorithms + their auto-derived 变异档位 — the canvas renders an
    editable variation table from this (registry-driven, new algorithms included)."""
    from .autotune import default_variation, phase_algorithms
    phases = phase_algorithms()
    out = {}
    for ph, algos in phases.items():
        out[ph] = [{"name": a, "label": REGISTRY[a].label or a,
                    "params": REGISTRY[a].params,
                    "default_variation": default_variation(a)} for a in algos]
    return {"phases": out}


@app.post("/autotune")
def autotune(spec: TuneSpec):
    """AutoTuner (L1): search workflow candidates, rank by quality/time/stability."""
    try:
        return run_autotune(spec)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")


# ============================ live workspace (agent ↔ canvas) ============================
class WorkspacePatch(BaseModel):
    graph: Optional[dict] = None
    bench: Optional[str] = None
    note: Optional[str] = None
    user_message: Optional[str] = None      # canvas chat → agent (appended to log)


@app.get("/workspace/{sid}")
def workspace_get(sid: str):
    """Current live workspace for a session (graph / bench / last result / autotune
    / revision). The canvas polls this (or the /stream SSE) to auto-refresh."""
    return WORKSPACE.snapshot(sid)


@app.post("/workspace/{sid}")
def workspace_patch(sid: str, patch: WorkspacePatch):
    """Canvas → workspace: the user's own edits, so an agent can get_canvas them back."""
    return WORKSPACE.update(sid, graph=patch.graph, bench=patch.bench, note=patch.note,
                            user_message=patch.user_message)


@app.get("/workspace/{sid}/stream")
async def workspace_stream(sid: str, request: Request):
    """SSE stream: emits the workspace snapshot whenever its revision changes, so a
    connected canvas re-renders automatically when an agent pushes a new graph."""
    async def gen():
        last = -1
        yield {"event": "snapshot", "data": json.dumps(WORKSPACE.snapshot(sid))}
        last = WORKSPACE.revision(sid)
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(1.0)
            rev = WORKSPACE.revision(sid)
            if rev != last:
                last = rev
                yield {"event": "update", "data": json.dumps(WORKSPACE.snapshot(sid))}
    return EventSourceResponse(gen())


# Mount the agent-facing MCP endpoint (streamable-HTTP) at /mcp.
app.mount("/mcp", mcp.streamable_http_app())


@app.get("/")
def index():
    path = os.path.join(_WEB, "index.html")
    if os.path.exists(path):
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="canvas not built")


def main():
    import uvicorn
    # 0.0.0.0 so the canvas/API/MCP are reachable from other hosts on the LAN;
    # override with OPTPLAT_HOST / OPTPLAT_PORT if needed.
    host = os.environ.get("OPTPLAT_HOST", "0.0.0.0")
    port = int(os.environ.get("OPTPLAT_PORT", "8003"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
