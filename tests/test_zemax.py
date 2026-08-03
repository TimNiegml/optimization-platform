"""Zemax/OpticStudio backend tests.

Everything runs against `optplat.zemax_sim` — a fake OpticStudio MCP server
speaking the real protocol with the real tool names — so the whole path
(MCP stdio -> lens data writes -> merit function read -> optimizer) is exercised
on any OS with no Zemax installed.
"""
import sys

import pytest

from optplat.backends import EvaluatorConfig, backend_catalog, build_evaluator
from optplat.hardware import SafetyLimits
from optplat.mcp_client import McpStdioClient
from optplat.orchestrator import Orchestrator
from optplat.vocs import VOCS, Objective, ObjectiveMode, Variable
from optplat.zemax import (
    CompositeEvaluator,
    Follower,
    MeritSpec,
    OperandRef,
    ZemaxBinding,
    ZemaxConnection,
    ZemaxEvaluator,
    ZemaxKnob,
    param_number,
)

FAKE_SERVER = [sys.executable, "-m", "optplat.zemax_sim"]
MASTER, RETURN = 3, 5          # surfaces in the fake design


@pytest.fixture
def client():
    c = McpStdioClient(FAKE_SERVER, timeout=30).start()
    yield c
    c.close()


def cb_binding(follow_mode="write", merit=None):
    """Drive Coordinate Break `MASTER`; Coordinate Break `RETURN` follows it."""
    def knob(param, low, high):
        return ZemaxKnob(
            surface=MASTER, kind="param", param=param, low=low, high=high,
            followers=[Follower(surface=RETURN, param=param, scale=-1.0, mode=follow_mode)],
        )
    return ZemaxBinding(
        knobs={
            "dec_x": knob("decenter_x", -1.0, 1.0),
            "dec_y": knob("decenter_y", -1.0, 1.0),
            "tilt_x": knob("tilt_x", -1.0, 1.0),
            "tilt_y": knob("tilt_y", -1.0, 1.0),
        },
        merit=merit or MeritSpec(total="merit"),
    )


def make_eval(client, **kw):
    binding = kw.pop("binding", None) or cb_binding()
    conn = ZemaxConnection(command=FAKE_SERVER, mode="extension", connect_on_bind=True)
    return ZemaxEvaluator(binding, conn, client=client, **kw)


# ---- parameter mapping ------------------------------------------------------
def test_coordinate_break_param_numbers():
    assert param_number("decenter_x") == 1
    assert param_number("Decenter Y") == 2
    assert param_number("tilt_x") == 3
    assert param_number("tilt_y") == 4
    assert param_number("tilt_z") == 5
    assert param_number("param7") == 7 and param_number(6) == 6
    with pytest.raises(ValueError):
        param_number("decenter_z")


def test_knob_unit_scaling():
    k = ZemaxKnob(surface=3, param="decenter_x", scale=0.001)   # micron -> mm
    assert k.to_zemax(250.0) == pytest.approx(0.25)


# ---- MCP transport ----------------------------------------------------------
def test_mcp_handshake_and_tool_call(client):
    names = {t["name"] for t in client.list_tools()}
    assert {"zemax_set_surface_parameter", "zemax_get_merit_function"} <= names
    assert client.call_tool("zemax_connect", {"mode": "extension"})["isConnected"] is True


# ---- writes: x -> Lens Data Editor -----------------------------------------
def test_writes_land_on_the_right_coordinate_break_parameters(client):
    ev = make_eval(client)
    ev.evaluate({"dec_x": 0.12, "dec_y": -0.08, "tilt_x": 0.3, "tilt_y": -0.2})
    read = client.call_tool("zemax_set_surface_parameter", {"surfaceNumber": MASTER})
    got = {p["number"]: p["value"] for p in read["parameters"]}
    assert got[1] == pytest.approx(0.12)     # Decenter X
    assert got[2] == pytest.approx(-0.08)    # Decenter Y
    assert got[3] == pytest.approx(0.30)     # Tilt About X
    assert got[4] == pytest.approx(-0.20)    # Tilt About Y


@pytest.mark.parametrize("mode", ["write", "pickup"])
def test_second_coordinate_break_follows(client, mode):
    ev = make_eval(client, binding=cb_binding(follow_mode=mode))
    x = {"dec_x": 0.4, "dec_y": -0.1, "tilt_x": 0.2, "tilt_y": 0.05}
    ev.evaluate(x)
    read = client.call_tool("zemax_set_surface_parameter", {"surfaceNumber": RETURN})
    got = {p["number"]: p["value"] for p in read["parameters"]}
    assert got[1] == pytest.approx(-0.4)     # mirrored decenter
    assert got[2] == pytest.approx(0.1)
    assert got[3] == pytest.approx(-0.2)     # mirrored tilt
    assert got[4] == pytest.approx(-0.05)


def test_thickness_knob_uses_set_surface(client):
    binding = ZemaxBinding(knobs={"t": ZemaxKnob(surface=MASTER, kind="thickness",
                                                 low=0.0, high=5.0)},
                           merit=MeritSpec(total="merit",
                                           operands={"thickness": OperandRef(type="TTHI")}))
    y = make_eval(client, binding=binding).evaluate({"t": 2.5})
    assert y["thickness"] == pytest.approx(2.5)


# ---- reads: Merit Function Editor -> y --------------------------------------
def test_objectives_from_merit_function_by_total_row_and_type(client):
    binding = cb_binding(merit=MeritSpec(
        total="merit",
        operands={"spot_by_row": OperandRef(row=2),
                  "spot_by_type": OperandRef(type="RSCE"),
                  "spot_contrib": OperandRef(row=2, field="contribution")},
    ))
    y = make_eval(client, binding=binding).evaluate(
        {"dec_x": 0.5, "dec_y": 0.0, "tilt_x": 0.0, "tilt_y": 0.0})
    assert y["spot_by_row"] == pytest.approx(y["spot_by_type"])
    assert y["merit"] == pytest.approx(y["spot_by_row"])
    assert y["spot_contrib"] == pytest.approx(y["spot_by_row"] ** 2)


def test_missing_operand_is_a_clear_error(client):
    binding = cb_binding(merit=MeritSpec(total=None,
                                         operands={"nope": OperandRef(type="MTFT")}))
    with pytest.raises(KeyError, match="nope"):
        make_eval(client, binding=binding).evaluate({"dec_x": 0.0})


# ---- guardrails -------------------------------------------------------------
def test_safety_limits_clamp_before_writing(client):
    ev = make_eval(client, safety=SafetyLimits({"dec_x": (-0.2, 0.2)}))
    ev.evaluate({"dec_x": 5.0})
    read = client.call_tool("zemax_set_surface_parameter", {"surfaceNumber": MASTER})
    got = {p["number"]: p["value"] for p in read["parameters"]}
    assert got[1] == pytest.approx(0.2)
    assert ev.history[-1]["dec_x"] == pytest.approx(0.2)


# ---- end to end: optimize an alignment through OpticStudio ------------------
def test_optimizes_coordinate_break_alignment(client):
    binding = cb_binding()
    vocs = binding.build_vocs()
    assert set(vocs.variables) == {"dec_x", "dec_y", "tilt_x", "tilt_y"}
    assert vocs.objectives["merit"].mode is ObjectiveMode.MINIMIZE

    ev = make_eval(client, binding=binding)
    start = ev.evaluate(vocs.initial_point())["merit"]

    pipeline = {"flow": [{
        "stage": "align",
        "algorithm": "coordinate_descent",
        "variables": ["dec_x", "dec_y", "tilt_x", "tilt_y"],
        "objective": "merit",
        "step": 0.4,
        "stop": {"max_iter": 400},
    }]}
    res = Orchestrator(vocs, ev, pipeline, eval_budget=2000).run()

    assert res["objectives"]["merit"] < start
    assert res["objectives"]["merit"] < 0.1          # near the aligned optimum
    assert res["state"]["dec_x"] == pytest.approx(0.12, abs=0.05)
    assert res["state"]["tilt_x"] == pytest.approx(0.30, abs=0.05)


# ---- backend registry / mixing with devices ---------------------------------
def test_zemax_is_a_registered_backend():
    assert {"function", "hardware_sim", "zemax", "composite"} <= set(backend_catalog())


def test_build_zemax_backend_from_config():
    binding = cb_binding()
    cfg = EvaluatorConfig(mode="zemax", safety=True,
                          connection={"command": FAKE_SERVER, "mode": "extension"},
                          binding=binding.model_dump())
    ev = build_evaluator(binding.build_vocs(), cfg)
    try:
        y = ev.evaluate({"dec_x": 0.12, "dec_y": -0.08, "tilt_x": 0.3, "tilt_y": -0.2})
        assert y["merit"] < 0.02
    finally:
        ev.close()


def test_zemax_backend_requires_binding():
    with pytest.raises(ValueError, match="binding"):
        build_evaluator(VOCS(), EvaluatorConfig(mode="zemax",
                                                connection={"command": FAKE_SERVER}))


def test_composite_runs_model_and_device_together(client):
    """Same evaluation drives an OpticStudio design and a (simulated) rig."""
    vocs = VOCS(
        variables={"dec_x": Variable(low=-1, high=1), "x1": Variable(low=-5, high=5),
                   "x2": Variable(low=-5, high=5), "x3": Variable(low=-5, high=5)},
        objectives={"merit": Objective(mode=ObjectiveMode.MINIMIZE),
                    "y1": Objective(mode=ObjectiveMode.MAXIMIZE)},
    )
    rig = build_evaluator(vocs, EvaluatorConfig(mode="hardware_sim", safety=True))
    sim = make_eval(client, binding=ZemaxBinding(
        knobs={"dec_x": ZemaxKnob(surface=MASTER, param="decenter_x", low=-1, high=1)}))
    comp = CompositeEvaluator({"zemax": (sim, ["dec_x"]),
                               "rig": (rig, ["x1", "x2", "x3"])})
    y = comp.evaluate({"dec_x": 0.12, "x1": 0.0, "x2": 0.0, "x3": 0.0})
    assert "merit" in y and "y1" in y
    assert len(comp.history) == 1
