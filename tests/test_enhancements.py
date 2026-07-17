"""Tests for the optical-tuning enhancements:
  * DampedSensitivity solver (multi-in/out target solve, SVD-regularised)
  * parallel/serial measurement-time grouping (read_seconds / Evaluator.groups)
  * AutoTuner random start point + configurable start_domain
  * API: custom start_point + channel_groups
"""
import math

import pytest

from optplat.autotune import TuneSpec, _random_start, run_autotune
from optplat.demo import demo_vocs, linear_sens_bench, nonlinear_sens_bench
from optplat.evaluator import Evaluator, read_seconds
from optplat.generators import DampedSensitivity
from optplat.graph import GraphRunner
from optplat.hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage


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
