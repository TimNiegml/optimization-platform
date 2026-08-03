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
    FollowerDesync,
    MeritSpec,
    OperandRef,
    RefreshSpec,
    ZemaxBinding,
    ZemaxConnection,
    ZemaxEvaluator,
    ZemaxKnob,
    param_number,
)
from optplat.zemax_inspect import (
    build_binding,
    detect_followers,
    inspect_system,
    knob_choices,
    operand_choices,
)

FAKE_SERVER = [sys.executable, "-m", "optplat.zemax_sim"]
# same fake design, but with the tilt-and-return pickup solves already authored
FAKE_SERVER_WITH_PICKUPS = FAKE_SERVER + ["--pickups"]
MASTER, RETURN = 3, 5          # surfaces in the fake design


@pytest.fixture
def client():
    c = McpStdioClient(FAKE_SERVER, timeout=30).start()
    yield c
    c.close()


@pytest.fixture
def authored_client():
    """A design whose return Coordinate Break already picks up from the master."""
    c = McpStdioClient(FAKE_SERVER_WITH_PICKUPS, timeout=30).start()
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


# ---- discovery: read the design, then pick surfaces/variables ---------------
def test_inspect_reads_the_lens_data_editor(client):
    snap = inspect_system(client)
    assert snap.units == "mm" and snap.n_surfaces == 7
    cb = [s for s in snap.surfaces if s.is_coordinate_break]
    assert [s.number for s in cb] == [MASTER, RETURN]
    assert snap.surface(MASTER).comment == "align in"
    assert snap.surface(2).material == "N-BK7"
    assert snap.surface(1).is_stop is True
    assert [op["type"] for op in operand_choices(snap)] == ["DMFS", "RSCE", "TTHI"]


def test_knob_choices_are_pickable_labelled_and_carry_current_values(client):
    client.call_tool("zemax_set_surface_parameter",
                     {"surfaceNumber": MASTER, "batchSet": "3:0.25"})
    choices = {c.id(): c for c in knob_choices(inspect_system(client))}

    tilt = choices[f"s{MASTER}.tilt_x"]
    assert tilt.label == "S3 · align in · Tilt About X"
    assert tilt.param_number == 3 and tilt.unit == "deg"
    assert tilt.current == pytest.approx(0.25)              # reads the design's value
    assert choices[f"s{MASTER}.decenter_x"].unit == "mm"    # follows the design units
    assert f"s{MASTER}.order" not in choices                # discrete, off by default
    assert f"s2.thickness" in choices                       # non-CB surfaces too

    knob = tilt.to_knob(low=-1.0, high=1.0)
    assert knob.surface == MASTER and knob.resolved_param() == 3


def test_followers_are_discovered_from_the_designs_pickup_solves(authored_client):
    """A tilt-and-return pair authored in OpticStudio needs no configuration."""
    snap = inspect_system(authored_client)
    followers = detect_followers(snap, MASTER, 3)
    assert len(followers) == 1
    f = followers[0]
    assert f.surface == RETURN and f.param == "tilt_x"
    assert f.scale == pytest.approx(-1.0) and f.mode == "pickup"

    choice = next(c for c in knob_choices(snap) if c.id() == f"s{MASTER}.decenter_y")
    assert [x.surface for x in choice.followers] == [RETURN]


def test_build_binding_from_picks_and_run_it(authored_client):
    snap = inspect_system(authored_client)
    binding = build_binding(snap, [
        {"name": "dec_x", "surface": MASTER, "param": "decenter_x", "low": -1, "high": 1},
        {"name": "tilt_x", "surface": MASTER, "param": "tilt_x", "low": -1, "high": 1},
    ], merit=MeritSpec(total="merit", operands={"rms_spot": OperandRef(type="RSCE")}))

    # followers came from the design, not from the caller
    assert [f.surface for f in binding.knobs["tilt_x"].followers] == [RETURN]
    vocs = binding.build_vocs()
    assert set(vocs.variables) == {"dec_x", "tilt_x"}

    ev = ZemaxEvaluator(binding, ZemaxConnection(command=FAKE_SERVER_WITH_PICKUPS),
                        client=authored_client)
    y = ev.evaluate({"dec_x": 0.12, "tilt_x": 0.30})
    assert set(y) == {"merit", "rms_spot"}
    # the design's own pickup moved the return surface; the platform verified it
    assert ev.last_followers[f"s{RETURN}.p1"] == pytest.approx(-0.12)
    assert ev.last_followers[f"s{RETURN}.p3"] == pytest.approx(-0.30)


def test_explicit_selection_overrides_discovered_followers(authored_client):
    snap = inspect_system(authored_client)
    binding = build_binding(snap, [
        {"name": "tilt_x", "surface": MASTER, "param": "tilt_x",
         "low": -1, "high": 1, "followers": []},          # drive the master alone
    ])
    assert binding.knobs["tilt_x"].followers == []


# ---- refresh: x changed -> followers / system / pupil are brought up to date --
def test_system_and_pupil_are_refreshed_after_writing_x(client):
    binding = cb_binding()
    binding.refresh = RefreshSpec(system=True, pupil=True)
    ev = make_eval(client, binding=binding)

    before = client.call_tool("zemax_status", {})["updates"]
    ev.evaluate({"dec_x": 0.1, "dec_y": 0.0, "tilt_x": 0.0, "tilt_y": 0.0})
    after = client.call_tool("zemax_status", {})["updates"]

    assert after > before                       # the system was recomputed
    assert ev.last_pupil["value"] == pytest.approx(10.0)     # pupil state captured
    assert ev.last_system["units"] == "mm"


def test_refresh_can_be_turned_off(client):
    binding = cb_binding()
    binding.refresh = RefreshSpec(system=False, pupil=False, followers=False)
    ev = make_eval(client, binding=binding)
    ev.evaluate({"dec_x": 0.1, "dec_y": 0.0, "tilt_x": 0.0, "tilt_y": 0.0})
    assert ev.last_followers == {} and ev.last_system == {}


@pytest.mark.parametrize("mode", ["write", "pickup"])
def test_follower_values_are_verified_and_recorded(client, mode):
    ev = make_eval(client, binding=cb_binding(follow_mode=mode))
    ev.evaluate({"dec_x": 0.4, "dec_y": -0.1, "tilt_x": 0.2, "tilt_y": 0.05})
    assert ev.last_followers[f"s{RETURN}.p1"] == pytest.approx(-0.4)
    assert ev.last_followers[f"s{RETURN}.p3"] == pytest.approx(-0.2)
    # follower values land in the platform's own history, next to x and y
    assert ev.history[-1][f"s{RETURN}.p1"] == pytest.approx(-0.4)


def test_a_follower_that_stops_tracking_is_an_error_not_bad_data(client):
    """Wrong pickup column / solve deleted in the .zmx -> fail loudly."""
    binding = cb_binding(follow_mode="pickup")
    # point the pickup at a cell that is not the master's, as a wrong column would
    binding.knobs["tilt_x"].followers[0].pickup_column = 6
    ev = make_eval(client, binding=binding)
    with pytest.raises(FollowerDesync, match="did not follow"):
        ev.evaluate({"dec_x": 0.0, "dec_y": 0.0, "tilt_x": 0.5, "tilt_y": 0.0})


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
