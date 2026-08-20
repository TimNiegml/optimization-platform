"""Tests for the optical-tuning enhancements:
  * DampedSensitivity solver (multi-in/out target solve, SVD-regularised)
  * parallel/serial measurement-time grouping (read_seconds / Evaluator.groups)
  * AutoTuner random start point + configurable start_domain
  * API: custom start_point + channel_groups
"""
import math
import os

import pytest
import numpy as np

from optplat.autotune import TuneSpec, _random_start, run_autotune
from optplat.demo import demo_vocs, linear_sens_bench, nonlinear_sens_bench
from optplat.evaluator import Evaluator, read_seconds
from optplat.generators import (
    CoordinateDescent,
    DampedSensitivity,
    GradientAscent,
    GridScan,
    NelderMead,
)
from optplat.generators import SurrogateFit
from optplat.graph import GraphRunner
from optplat.hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage
from optplat.userdev import load_device
from optplat.workspace import WorkspaceStore

_DEVICE = os.path.join(os.path.dirname(__file__), "..", "examples", "device_template.py")


def test_dataframe_like_measurement_is_normalized_without_pandas_dependency():
    class FrameLike:
        def to_numpy(self):
            return np.array([[1.0, 2.0], [3.0, 4.0]])

    ev = Evaluator(lambda x: {"table": FrameLike()})
    assert ev.evaluate({}, channels={"table"})["table"] == [[1.0, 2.0], [3.0, 4.0]]


def test_external_device_accepts_and_averages_matrix_meter():
    from optplat.userdev import Axis, DeviceSpec, Meter

    readings = iter(([[1, 2], [3, 4]], [[3, 4], [5, 6]]))
    spec = DeviceSpec([Axis("x1", 0, 1)], [
        Meter("camera", lambda: next(readings), value_type="matrix")])
    assert spec.vocs().objectives["camera"].value_type.value == "matrix"
    result = spec.evaluator(averages=2).evaluate({"x1": 0.5}, channels={"camera"})
    assert result["camera"] == [[2.0, 3.0], [4.0, 5.0]]


def test_external_device_accepts_and_averages_vector_meter():
    from optplat.userdev import Axis, DeviceSpec, Meter

    readings = iter(([1, 2, 3], [3, 4, 5]))
    axis = Axis("x1", 0, 1, pos=0.25, device="stage", param="A")
    spec = DeviceSpec([axis], [Meter("spectrum", lambda: next(readings), value_type="vector",
                                           display_name="光谱", unit="dB", shape=(3,), dtype="float64")])
    assert spec.vocs().objectives["spectrum"].value_type.value == "vector"
    assert spec.evaluator(averages=2).evaluate({"x1": 0.5})["spectrum"] == [2.0, 3.0, 4.0]
    assert spec.info()["axes"][0] == {
        "name": "x1", "low": 0.0, "high": 1.0, "pos": 0.5,
        "resolution": None, "device": "stage", "param": "A"}
    meter = spec.info()["meters"][0]
    assert (meter["display_name"], meter["unit"], meter["shape"], meter["dtype"]) == (
        "光谱", "dB", [3], "float64")


# ---------------- DampedSensitivity ----------------
SENS = {"y1": {"x1": 0.8, "x2": 0.3}, "y2": {"x1": 0.2, "x2": -0.6}}


def _solve_graph(targets, damping=0.8):
    return {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "solve", "type": "algorithm", "data": {
                "algorithm": "damped_sensitivity", "variables": ["x1", "x2"],
                "objective": "y1", "targets": targets, "sensitivity": SENS,
                "damping": damping, "stop": {"max_iter": 80}}},
            {"id": "end", "type": "end"},
        ],
        "edges": [{"source": "start", "target": "solve"},
                  {"source": "solve", "target": "end"}],
    }


def test_damped_sensitivity_drives_multiple_targets():
    vocs = demo_vocs()
    ev = Evaluator(linear_sens_bench)
    res = GraphRunner(vocs, ev, _solve_graph({"y1": 2.0, "y2": 0.0})).run()
    assert abs(res["objectives"]["y1"] - 2.0) < 1e-2
    assert abs(res["objectives"]["y2"] - 0.0) < 1e-2
    assert "damped_sensitivity" in res["fits"]         # residual formula recorded


def test_damped_sensitivity_only_reads_target_channels():
    # a solver targeting y1 alone reads y1 every iteration; y2 is only touched by
    # the one-off init prime (which reads all channels), never during the stage.
    vocs = demo_vocs()
    ev = Evaluator(linear_sens_bench, costs={"y1": 1.0, "y2": 2.0})
    GraphRunner(vocs, ev, _solve_graph({"y1": 2.0})).run()
    assert ev.reads.get("y1", 0) > 2                     # iterated on the target
    assert ev.reads.get("y2", 0) <= 1                    # only the init prime, if any


def test_damped_sensitivity_solves_noisy_nonlinear_to_zero():
    # near-linear plant (mild cubic far from solution), target y=0, WITH noise —
    # damped least squares + averaging still drives both outputs to ~0.
    vocs = demo_vocs()
    graph = _solve_graph({"y1": 0.0, "y2": 0.0}, damping=0.6)
    graph["nodes"][1]["data"]["max_solves"] = 60
    stage = SimulatedStage(vocs.initial_point())
    meter = SimulatedMeter(stage, nonlinear_sens_bench, noise=0.01, seed=1)
    ev = HardwareEvaluator(stage, meter, averages=5)
    res = GraphRunner(vocs, ev, graph, start_point={"x1": 5.0, "x2": -3.0, "x3": 0.7}).run()
    assert abs(res["objectives"]["y1"]) < 0.05
    assert abs(res["objectives"]["y2"]) < 0.05


def test_damped_sensitivity_regularised_inverse_is_stable():
    # a rank-deficient (singular) sensitivity must NOT blow up — damped SVD stays bounded
    vocs = demo_vocs()
    gen = DampedSensitivity(vocs, ["x1", "x2"], "y1",
                            sensitivity={"y1": {"x1": 1.0, "x2": 1.0},
                                         "y2": {"x1": 1.0, "x2": 1.0}},
                            targets={"y1": 0.5, "y2": 0.5}, reg=1e-6)
    dx = gen._solve([10.0, 10.0])
    assert all(math.isfinite(v) for v in dx)
    assert max(abs(v) for v in dx) < 1e6                # bounded, not exploded


def test_damped_sensitivity_max_solves_counts_actuator_corrections():
    """One configured solve means one Δx correction plus a verification read."""
    vocs = demo_vocs()
    gen = DampedSensitivity(
        vocs, ["x1"], "y1", sensitivity={"y1": {"x1": 1.0}},
        targets={"y1": 1.0}, damping=1.0, max_solves=1,
    )
    gen.set_base({"x1": 0.0, "x2": 0.0, "x3": 0.0})
    first = gen.ask()
    gen.observe(first, {"y1": 0.0})
    corrected = gen.ask()
    assert corrected["x1"] > first["x1"]
    assert not gen.done
    gen.observe(corrected, {"y1": 0.5})
    assert gen.done
    assert gen._solves == 1


def test_damped_sensitivity_rejects_invalid_iteration_settings():
    vocs = demo_vocs()
    with pytest.raises(ValueError, match="max_solves"):
        DampedSensitivity(vocs, ["x1"], "y1", sensitivity=[[1.0]],
                          targets={"y1": 1.0}, max_solves=0)


# ---------------- relative scan window + per-axis hyperparameters ----------------
def test_grid_scan_relative_window_centres_on_start():
    vocs = demo_vocs()
    g = GridScan(vocs, ["x1"], "y1", n_per_axis=5, span_frac=0.25)
    g.set_base({"x1": 2.0, "x2": 0.0, "x3": 0.0})
    xs = []
    for _ in range(5):
        x = g.ask()["x1"]; xs.append(x); g.tell({"x1": x}, 0.0)
    rng = vocs.variables["x1"].high - vocs.variables["x1"].low
    half = 0.5 * 0.25 * rng
    assert min(xs) >= 2.0 - half - 1e-9 and max(xs) <= 2.0 + half + 1e-9
    assert min(xs) < 2.0 < max(xs)                      # window is around the start point


def test_grid_scan_absolute_by_default():
    vocs = demo_vocs()
    g = GridScan(vocs, ["x1"], "y1", n_per_axis=3)       # span_frac defaults to 0
    g.set_base({"x1": 2.0, "x2": 0.0, "x3": 0.0})
    xs = [g.ask()["x1"] for _ in range(3)]
    assert min(xs) == vocs.variables["x1"].low and max(xs) == vocs.variables["x1"].high


def test_grid_scan_explicit_axis_range_overrides_relative_window():
    vocs = demo_vocs()
    g = GridScan(vocs, ["x1"], "y1", n_per_axis=3, span_frac=0.1,
                 search_ranges={"x1": [1.0, 2.0]})
    g.set_base({"x1": 4.0, "x2": 0.0, "x3": 0.0})
    xs = [g.ask()["x1"] for _ in range(3)]
    assert xs == pytest.approx([1.0, 1.5, 2.0])


def test_coordinate_descent_per_axis_step():
    vocs = demo_vocs()
    g = CoordinateDescent(vocs, ["x1", "x2"], "y1", steps={"x1": 0.5})
    assert g._step["x1"] == 0.5                          # explicit per-axis override
    assert g._step["x2"] == pytest.approx(
        0.25 * (vocs.variables["x2"].high - vocs.variables["x2"].low))   # frac fallback


def test_coordinate_descent_can_stop_without_reducing_step():
    vocs = demo_vocs()
    g = CoordinateDescent(vocs, ["x1"], "y1", steps={"x1": 0.5}, refine_step=False)
    g.set_base({"x1": 0.0, "x2": 0.0, "x3": 0.0})
    p = g.ask(); g.tell(p, 1.0)
    for _ in range(2):
        p = g.ask(); g.tell(p, 0.0)
    assert g.done
    assert g._step["x1"] == pytest.approx(0.5)


def test_coordinate_descent_shrink_patience_and_factor():
    vocs = demo_vocs()
    g = CoordinateDescent(vocs, ["x1"], "y1", steps={"x1": 1.0},
                          shrink_patience=2, shrink_factor=0.25)
    g.set_base({"x1": 0.0, "x2": 0.0, "x3": 0.0})
    p = g.ask(); g.tell(p, 1.0)
    for _ in range(2):
        p = g.ask(); g.tell(p, 0.0)
    assert g._step["x1"] == pytest.approx(1.0)
    for _ in range(2):
        p = g.ask(); g.tell(p, 0.0)
    assert g._step["x1"] == pytest.approx(0.25)


def test_gradient_can_stop_on_first_failed_line_step_without_shrinking():
    vocs = demo_vocs()
    g = GradientAscent(vocs, ["x1"], "y1", shrink_on_fail=False)
    g.set_base({"x1": 0.0, "x2": 0.0, "x3": 0.0})
    p = g.ask(); g.tell(p, 0.0)       # base
    p = g.ask(); g.tell(p, 1.0)       # non-zero measured gradient
    p = g.ask(); g.tell(p, 0.0)       # line step fails
    assert g.done


def test_nelder_mead_per_axis_simplex():
    vocs = demo_vocs()
    g = NelderMead(vocs, ["x1", "x2"], "y1", init_steps={"x2": 0.7})
    assert g._init_step["x2"] == 0.7
    assert g._init_step["x1"] == pytest.approx(
        0.1 * (vocs.variables["x1"].high - vocs.variables["x1"].low))


def test_fit_span_frac_windows_around_start():
    vocs = demo_vocs()
    g = SurrogateFit(vocs, ["x1"], "y1", n_samples=5, span_frac=0.25)
    g.set_base({"x1": 4.0, "x2": 0.0, "x3": 0.0})
    xs = []
    for _ in range(5):                                  # the 5 design (sampling) points
        x = g.ask()["x1"]; xs.append(x); g.tell({"x1": x}, 0.5)
    half = 0.5 * 0.25 * (vocs.variables["x1"].high - vocs.variables["x1"].low)
    assert min(xs) >= 4.0 - half - 1e-9 and max(xs) <= 4.0 + half + 1e-9


# ---------------- external device interface ----------------
def test_device_autoreads_x_y_and_starts_from_current_pose():
    dev = load_device(_DEVICE)
    vocs = dev.vocs()
    assert list(vocs.variables) == ["x1", "x2"]         # platform auto-reads n_x
    assert list(vocs.objectives) == ["y1", "y2"]        # ... and n_y
    assert vocs.objectives["y2"].mode.value == "target" and vocs.objectives["y2"].target == 0.0
    assert dev.current_point() == {"x1": 5.0, "x2": -3.0}   # start = current axis pose
    graph = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "sc", "type": "algorithm", "data": {
                "algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
                "n_per_axis": 7, "span_frac": 0.6, "stop": {"target": "y1>0.2"}}},
            {"id": "r", "type": "algorithm", "data": {
                "algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1"}},
            {"id": "e", "type": "end"}],
        "edges": [{"source": "s", "target": "sc"}, {"source": "sc", "target": "r"},
                  {"source": "r", "target": "e"}]}
    res = GraphRunner(vocs, dev.evaluator(), graph, start_point=dev.current_point()).run()
    assert res["objectives"]["y1"] > 0.95               # found the coupling peak
    assert abs(res["state"]["x1"] - 2.0) < 0.15 and abs(res["state"]["x2"] + 1.0) < 0.15


def test_device_evaluator_selective_read():
    dev = load_device(_DEVICE)
    ev = dev.evaluator()
    y = ev.evaluate({"x1": 2.0, "x2": -1.0}, channels=["y1"])
    assert set(y) == {"y1"}                             # only y1 requested
    assert ev.reads.get("y2", 0) == 0                   # y2.get() never called


# ---------------- workspace canvas→agent chat ----------------
def test_workspace_user_message_and_note_log():
    ws = WorkspaceStore()
    ws.update("s1", user_message="把 y1 调到 2")
    snap = ws.snapshot("s1")
    assert snap["messages"][-1]["role"] == "user" and "2" in snap["messages"][-1]["text"]
    ws.update("s1", note="已完成 y1→2")
    roles = [m["role"] for m in ws.snapshot("s1")["messages"]]
    assert roles == ["user", "agent"]


def test_workspace_inbox_polls_unread_once():
    ws = WorkspaceStore()
    ws.update("s2", user_message="第一条")
    ws.update("s2", user_message="第二条")
    first = ws.poll_user_messages("s2")               # drains both unread
    assert [m["text"] for m in first] == ["第一条", "第二条"]
    assert ws.poll_user_messages("s2") == []          # already read → empty
    ws.update("s2", user_message="第三条")             # a new one arrives
    assert [m["text"] for m in ws.poll_user_messages("s2")] == ["第三条"]
    # non-consuming peek leaves it unread
    ws.update("s2", user_message="第四条")
    assert ws.poll_user_messages("s2", mark_read=False)
    assert ws.poll_user_messages("s2", mark_read=False)


# ---------------- measurement-time grouping ----------------
def test_read_seconds_serial_and_parallel():
    costs = {"y1": 1.0, "y2": 2.0, "y3": 4.0}
    # no groups -> everything serial (sum)
    assert read_seconds(["y1", "y2", "y3"], costs) == 7.0
    # y1,y2 parallel (same group = max), y3 its own serial group
    groups = {"y1": "g1", "y2": "g1", "y3": "g2"}
    assert read_seconds(["y1", "y2", "y3"], costs, groups) == max(1.0, 2.0) + 4.0
    # all in one group -> just the max
    assert read_seconds(["y1", "y2", "y3"], costs, {"y1": "g", "y2": "g", "y3": "g"}) == 4.0


def test_evaluator_groups_reduce_sim_seconds():
    ev_serial = Evaluator(linear_sens_bench, costs={"y1": 1.0, "y2": 2.0})
    ev_serial.evaluate({"x1": 0, "x2": 0, "x3": 0})
    ev_par = Evaluator(linear_sens_bench, costs={"y1": 1.0, "y2": 2.0},
                       groups={"y1": "g", "y2": "g"})
    ev_par.evaluate({"x1": 0, "x2": 0, "x3": 0})
    assert ev_serial.sim_seconds == 3.0                 # 1 + 2 serial
    assert ev_par.sim_seconds == 2.0                    # max(1,2) parallel


# ---------------- AutoTuner random start ----------------
def test_random_start_within_domain():
    vocs = demo_vocs()
    spec = TuneSpec(start_domain={"x1": [0.0, 1.0]})
    pt = _random_start(vocs, spec, seed=3)
    assert 0.0 <= pt["x1"] <= 1.0
    # unspecified vars fall back to full VOCS range
    assert vocs.variables["x2"].low <= pt["x2"] <= vocs.variables["x2"].high


def test_autotune_random_start_runs():
    res = run_autotune(TuneSpec(bench="single_peak", n_trials=2, noise_levels=[0.0],
                                max_candidates=5, random_start=True,
                                start_domain={"x1": [1.0, 3.0], "x2": [-2.0, 0.0]}))
    assert res["n_candidates"] > 0
    assert all("quality" in r for r in res["ranked"])


# ---------------- API: start_point + channel_groups ----------------
def test_api_start_point_and_groups():
    httpx = pytest.importorskip("httpx")  # noqa: F841
    from fastapi.testclient import TestClient
    from optplat.api import app
    client = TestClient(app)
    graph = _solve_graph({"y1": 2.0, "y2": 0.0})
    body = {
        "graph": graph,
        "vocs": None,
        "evaluator": {"mode": "function", "bench": "linear_sens",
                      "channel_costs": {"y1": 1.0, "y2": 2.0},
                      "channel_groups": {"y1": "g", "y2": "g"}},
        "start_point": {"x1": -1.0, "x2": 2.0, "x3": 0.5},
    }
    r = client.post("/run/graph", json=body)
    assert r.status_code == 200, r.text
    d = r.json()
    assert abs(d["objectives"]["y1"] - 2.0) < 1e-2
    # parallel group -> per-read time is max(1,2)=2, so sim_seconds is a multiple of 2
    assert d["sim_seconds"] > 0


# ---------------- shared acquisition (one instrument read -> several y) --------
def _pm_device(counter):
    """A 2-channel power meter: ONE trigger yields power1 + power2."""
    from optplat.userdev import Axis, DeviceSpec, Meter, Source

    ax = Axis("x1", low=-1.0, high=1.0, pos=0.0)

    def read_both():
        counter.append(1)                       # count real instrument triggers
        p = ax.get()
        return {"power1": 1.0 - p * p, "power2": 0.5 * p}

    pm = Source("pm", read_both, cost=1.0, device="双通道光功率计")
    other = Meter("y2", lambda: 0.0, cost=2.0)
    return DeviceSpec([ax], [pm.meter("y1", "power1"),
                             pm.meter("y3", "power2", mode="target", target=0.0),
                             other])


def test_source_triggers_instrument_once_per_acquisition():
    calls = []
    ev = _pm_device(calls).evaluator()
    y = ev.evaluate({"x1": 0.5}, channels=["y1", "y3"])
    assert set(y) == {"y1", "y3"}
    assert len(calls) == 1                      # both channels from ONE trigger
    assert y["y1"] == pytest.approx(0.75) and y["y3"] == pytest.approx(0.25)


def test_source_channels_share_one_acquisition_but_averaging_stays_independent():
    calls = []
    ev = _pm_device(calls).evaluator(averages=4)
    ev.evaluate({"x1": 0.5}, channels=["y1", "y3"])
    assert len(calls) == 4                      # 4 passes, not 4x2 and not 1


def test_source_unused_channel_never_triggers_it():
    calls = []
    ev = _pm_device(calls).evaluator()
    ev.evaluate({"x1": 0.5}, channels=["y2"])   # y2 is a different instrument
    assert calls == []                          # the power meter is never read


def test_source_channels_are_billed_once_not_per_channel():
    dev = _pm_device([])
    ev = dev.evaluator()
    ev.evaluate({"x1": 0.0}, channels=["y1", "y3"])
    both = ev.sim_seconds                       # same source => concurrent => max
    ev2 = dev.evaluator()
    ev2.evaluate({"x1": 0.0}, channels=["y1"])
    assert both == pytest.approx(ev2.sim_seconds)   # 2 channels cost the same 1 read
    ev3 = dev.evaluator()
    ev3.evaluate({"x1": 0.0}, channels=["y1", "y2"])
    assert ev3.sim_seconds == pytest.approx(3.0)    # different instruments => serial
