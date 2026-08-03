"""Zemax OpticStudio example: align a Coordinate Break against the Merit Function.

    python run_zemax.py                    # offline: runs against the fake server
    python run_zemax.py "C:/path/OpticStudioMCPServer.exe" "C:/designs/lens.zmx"

x = surface 3's Coordinate Break (Decenter X/Y, Tilt About X/Y); surface 5 is the
return Coordinate Break and follows it with scale -1. y = the Merit Function
Editor (total merit + the RSCE operand). Nothing below is Zemax-specific except
the two config objects — swap them for a `hardware_sim` / real-instrument backend
and the same pipeline drives a motor stage instead.
"""
from __future__ import annotations

import sys

from optplat import Orchestrator
from optplat.zemax import (
    Follower,
    MeritSpec,
    OperandRef,
    ZemaxBinding,
    ZemaxConnection,
    ZemaxEvaluator,
    ZemaxKnob,
)

MASTER_CB, RETURN_CB = 3, 5


def build_binding() -> ZemaxBinding:
    def knob(param: str, low: float, high: float) -> ZemaxKnob:
        return ZemaxKnob(
            surface=MASTER_CB, kind="param", param=param, low=low, high=high,
            # the return Coordinate Break mirrors the master (tilt/decenter-and-return).
            # mode="pickup" instead lets OpticStudio maintain it with a Pickup solve.
            followers=[Follower(surface=RETURN_CB, param=param, scale=-1.0, mode="write")],
        )

    return ZemaxBinding(
        knobs={
            "dec_x": knob("decenter_x", -1.0, 1.0),     # PARM 1, mm
            "dec_y": knob("decenter_y", -1.0, 1.0),     # PARM 2, mm
            "tilt_x": knob("tilt_x", -1.0, 1.0),        # PARM 3, deg
            "tilt_y": knob("tilt_y", -1.0, 1.0),        # PARM 4, deg
        },
        merit=MeritSpec(total="merit", operands={"rms_spot": OperandRef(type="RSCE")}),
    )


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
        connection = ZemaxConnection(command=[sys.executable, "-m", "optplat.zemax_sim"],
                                     mode="extension")
        print("no server path given → using the built-in fake OpticStudio server\n")

    binding = build_binding()
    vocs = binding.build_vocs()                        # variables+objectives from the binding
    evaluator = ZemaxEvaluator(binding, connection)
    try:
        res = Orchestrator(vocs, evaluator, PIPELINE, eval_budget=3000).run()
    finally:
        evaluator.close()

    print("\n".join(res["events"]))
    print("\nfinal操作点:", {k: round(v, 4) for k, v in res["state"].items()})
    print("merit function:", {k: round(v, 5) for k, v in res["objectives"].items()})
    print("evaluations:", res["n_evals"])


if __name__ == "__main__":
    main()
