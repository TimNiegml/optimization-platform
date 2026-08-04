"""Zemax OpticStudio example: align a Coordinate Break against the Merit Function.

    python run_zemax.py                    # offline: runs against the fake server
    python run_zemax.py "C:/path/OpticStudioMCPServer.exe" "C:/designs/lens.zmx"

The variables are *picked out of the design*, not hand-written: the platform
reads the Lens Data Editor, and the surface + parameter are selected by name
(`S3 · align in · Tilt About X`). Whatever already follows those cells in the
.zmx — the return Coordinate Break's pickup solves — is discovered and attached
automatically. `python -m optplat.zemax_inspect` prints the same pick-list.

y comes from the Merit Function Editor (total merit + the RSCE operand).
Nothing below is Zemax-specific except the connection — swap it for a
`hardware_sim` / real-instrument backend and the same pipeline drives a motor
stage instead.
"""
from __future__ import annotations

import sys

from optplat import Orchestrator
from optplat.zemax import (
    MeritSpec,
    OperandRef,
    ZemaxBinding,
    ZemaxConnection,
    ZemaxEvaluator,
)
from optplat.zemax_inspect import build_binding, inspect_system, knob_choices

MASTER_CB = 3            # the Coordinate Break we align ("align in")

# What the user picked in the UI (or would tick on the canvas).
SELECTIONS = [
    {"name": "dec_x", "surface": MASTER_CB, "param": "decenter_x", "low": -1.0, "high": 1.0},
    {"name": "dec_y", "surface": MASTER_CB, "param": "decenter_y", "low": -1.0, "high": 1.0},
    {"name": "tilt_x", "surface": MASTER_CB, "param": "tilt_x", "low": -1.0, "high": 1.0},
    {"name": "tilt_y", "surface": MASTER_CB, "param": "tilt_y", "low": -1.0, "high": 1.0},
]
MERIT = MeritSpec(total="merit", operands={"rms_spot": OperandRef(type="RSCE")})


PIPELINE = {
    "until": "merit < 0.02",                          # global early stop
    "flow": [
        {"stage": "coarse", "algorithm": "grid_scan",
         "variables": ["dec_x", "dec_y"], "objective": "merit",
         "n_per_axis": 5, "stop": {"target": "merit < 0.6"}},
        {"stage": "align", "algorithm": "coordinate_descent",
         "variables": ["dec_x", "dec_y", "tilt_x", "tilt_y"], "objective": "merit",
         "step": 0.4, "stop": {"max_iter": 400}},
    ],
}


def main() -> None:
    if len(sys.argv) > 1:                              # real OpticStudio
        connection = ZemaxConnection(command=[sys.argv[1]], mode="extension",
                                     file=sys.argv[2] if len(sys.argv) > 2 else None)
    else:                                              # offline stand-in
        connection = ZemaxConnection(
            command=[sys.executable, "-m", "optplat.zemax_sim", "--pickups"],
            mode="extension")
        print("no server path given → using the built-in fake OpticStudio server\n")

    # 1. read the design, 2. pick surfaces/parameters, 3. run.
    probe = ZemaxEvaluator(ZemaxBinding(), connection)      # a session to read through
    probe.bind()
    snap = inspect_system(probe.client)
    print(f"设计: {snap.title or snap.file} · {snap.n_surfaces} 面 · {snap.units}")
    for c in knob_choices(snap):
        if c.surface == MASTER_CB and c.kind == "param":
            follow = ", ".join(f"面{f.surface}(×{f.scale:g})" for f in c.followers)
            print(f"  可选: {c.label}  当前={c.current:g} {c.unit}"
                  + (f"  伴随→ {follow}" if follow else ""))

    binding = build_binding(snap, SELECTIONS, MERIT)   # followers come from the design
    vocs = binding.build_vocs()                        # variables+objectives too
    evaluator = ZemaxEvaluator(binding, connection, client=probe.client)
    try:
        res = Orchestrator(vocs, evaluator, PIPELINE, eval_budget=3000).run()
    finally:
        probe.close()

    print("\n".join(res["events"]))
    print("\nfinal操作点:", {k: round(v, 4) for k, v in res["state"].items()})
    print("伴随变量(回读确认):", {k: round(v, 4) for k, v in evaluator.last_followers.items()})
    print("merit function:", {k: round(v, 5) for k, v in res["objectives"].items()})
    print("evaluations:", res["n_evals"])


if __name__ == "__main__":
    main()
