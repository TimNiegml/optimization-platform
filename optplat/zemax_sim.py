"""A fake OpticStudio MCP server — lets the Zemax path run/test with no Zemax.

    python -m optplat.zemax_sim          # speaks MCP stdio on stdin/stdout

It implements just the handful of tools `ZemaxEvaluator` uses, with the same
names and argument shapes as the real server
(https://github.com/zym1998year/OpticStudioMCPServer):

    zemax_connect  zemax_status  zemax_open_file
    zemax_set_surface  zemax_set_surface_parameter  zemax_set_surface_solve
    zemax_get_merit_function

Behind them is a toy "design": a Coordinate Break whose decenter/tilt drive an
RMS spot size, plus a second Coordinate Break that is supposed to mirror it
(tilt/decenter-and-return). If the return surface does not mirror the first, the
spot degrades — so a follow rule that is wrong is visible in the merit function,
exactly like in a real design. Pickup solves are honoured too.

This is a development/CI stand-in, not an optical model.
"""
from __future__ import annotations

import json
import math
import sys
from typing import Any

PROTOCOL_VERSION = "2025-06-18"

# The "aligned" answer the optimizer has to find (Zemax units).
BEST = {1: 0.12, 2: -0.08, 3: 0.30, 4: -0.20, 5: 0.0}
WEIGHT = {1: 1.0, 2: 1.0, 3: 0.6, 4: 0.6, 5: 0.2}
MASTER_CB = 3          # the driven Coordinate Break
RETURN_CB = 5          # the one that must follow it (scale -1)


class FakeSystem:
    def __init__(self) -> None:
        self.connected = False
        self.mode = "standalone"
        self.file: str | None = None
        self.params: dict[int, dict[int, float]] = {}
        self.surfaces: dict[int, dict[str, float]] = {}
        # (surface, param) -> (src_surface, src_param, scale, offset)
        self.pickups: dict[tuple[int, int], tuple[int, int, float, float]] = {}

    # ---- lens data ----
    def param(self, surface: int, number: int) -> float:
        return self.params.get(surface, {}).get(number, 0.0)

    def set_param(self, surface: int, number: int, value: float) -> float:
        self.params.setdefault(surface, {})[number] = float(value)
        self._apply_pickups()
        return self.param(surface, number)

    def _apply_pickups(self) -> None:
        for (surf, num), (src_s, src_p, scale, offset) in self.pickups.items():
            self.params.setdefault(surf, {})[num] = scale * self.param(src_s, src_p) + offset

    # ---- merit function ----
    def rms_spot(self) -> float:
        err = sum(w * (self.param(MASTER_CB, n) - BEST[n]) ** 2 for n, w in WEIGHT.items())
        # the return CB must mirror the master; residual misalignment hurts
        mism = sum((self.param(RETURN_CB, n) + self.param(MASTER_CB, n)) ** 2
                   for n in (1, 2, 3, 4, 5))
        return math.sqrt(err + 2.0 * mism) + 0.010

    def merit_function(self) -> dict[str, Any]:
        spot = self.rms_spot()
        thickness = self.surfaces.get(MASTER_CB, {}).get("thickness", 0.0)
        operands = [
            {"row": 1, "type": "DMFS", "target": 0.0, "weight": 0.0,
             "value": 0.0, "contribution": 0.0},
            {"row": 2, "type": "RSCE", "target": 0.0, "weight": 1.0,
             "value": spot, "contribution": spot * spot},
            {"row": 3, "type": "TTHI", "target": 0.0, "weight": 0.0,
             "value": thickness, "contribution": 0.0},
        ]
        return {"success": True, "error": None, "totalMerit": spot,
                "numberOfOperands": len(operands), "operands": operands}


SYS = FakeSystem()


# ---- tools ------------------------------------------------------------------
def t_connect(a: dict) -> dict:
    SYS.connected = True
    SYS.mode = a.get("mode", "standalone")
    return {"success": True, "isConnected": True, "mode": SYS.mode}


def t_status(a: dict) -> dict:
    return {"success": True, "isConnected": SYS.connected, "file": SYS.file}


def t_open_file(a: dict) -> dict:
    SYS.file = a.get("filePath")
    return {"success": True, "filePath": SYS.file}


def t_set_surface(a: dict) -> dict:
    surface = int(a["surfaceNumber"])
    row = SYS.surfaces.setdefault(surface, {})
    for key in ("radius", "thickness", "conic", "semiDiameter"):
        if a.get(key) is not None:
            row[key if key != "semiDiameter" else "semi_diameter"] = float(a[key])
    return {"success": True, "surfaceNumber": surface, **row}


def t_set_surface_parameter(a: dict) -> dict:
    surface = int(a["surfaceNumber"])
    batch = a.get("batchSet")
    entries = []
    if batch:
        for pair in str(batch).split(","):
            num, _, val = pair.partition(":")
            entries.append({"number": int(num),
                            "value": SYS.set_param(surface, int(num), float(val))})
    elif a.get("value") is not None and int(a.get("parameterNumber", 0)) > 0:
        num = int(a["parameterNumber"])
        entries.append({"number": num,
                        "value": SYS.set_param(surface, num, float(a["value"]))})
    else:                                            # read mode
        entries = [{"number": n, "value": SYS.param(surface, n)} for n in range(1, 9)]
    return {"success": True, "surfaceNumber": surface,
            "surfaceType": "CoordinateBreak", "parameters": entries}


def t_set_surface_solve(a: dict) -> dict:
    prop = str(a["property"])
    if str(a.get("solveType", "")).lower() == "pickup" and prop.startswith("param"):
        SYS.pickups[(int(a["surfaceNumber"]), int(prop[5:]))] = (
            int(a["pickupSurface"]),
            int(a.get("pickupColumn") or int(prop[5:])),
            float(a.get("scaleFactor", 1.0) or 1.0),
            float(a.get("offset", 0.0) or 0.0),
        )
        SYS._apply_pickups()
    return {"success": True, "surfaceNumber": a["surfaceNumber"], "property": prop,
            "solveType": a.get("solveType")}


def t_get_merit_function(a: dict) -> dict:
    return SYS.merit_function()


TOOLS = {
    "zemax_connect": t_connect,
    "zemax_status": t_status,
    "zemax_open_file": t_open_file,
    "zemax_set_surface": t_set_surface,
    "zemax_set_surface_parameter": t_set_surface_parameter,
    "zemax_set_surface_solve": t_set_surface_solve,
    "zemax_get_merit_function": t_get_merit_function,
}


# ---- MCP stdio loop ---------------------------------------------------------
def _handle(msg: dict) -> dict | None:
    method, req_id = msg.get("method"), msg.get("id")
    if req_id is None:
        return None                                  # notification
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-opticstudio", "version": "0.1"}}
    if method == "tools/list":
        return {"tools": [{"name": n, "description": f"fake {n}",
                           "inputSchema": {"type": "object"}} for n in TOOLS]}
    if method == "tools/call":
        params = msg.get("params", {})
        fn = TOOLS.get(params.get("name"))
        if fn is None:
            return {"isError": True,
                    "content": [{"type": "text", "text": f"unknown tool {params.get('name')}"}]}
        out = fn(params.get("arguments") or {})
        return {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}
    raise KeyError(method)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        try:
            result = _handle(msg)
        except Exception as e:                       # surface as a JSON-RPC error
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32601, "message": f"{type(e).__name__}: {e}"}}) + "\n")
            sys.stdout.flush()
            continue
        if result is None:
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
