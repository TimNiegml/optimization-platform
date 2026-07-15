"""P1b regression tests: Bayesian generator, hardware adapter, SQLite store."""
import os
import tempfile

from optplat import Orchestrator
from optplat.demo import TWO_PHASE_PIPELINE, demo_vocs, optical_bench
from optplat.evaluator import Evaluator
from optplat.hardware import (
    HardwareEvaluator,
    SafetyLimits,
    SafetyViolation,
    SimulatedMeter,
    SimulatedStage,
)
from optplat.store import SQLiteStore, rollback_to_best


def test_bayesian_generator_approaches_peak():
    vocs = demo_vocs()
    pipe = {"flow": [{"stage": "bo", "algorithm": "bayesian", "variables": ["x1", "x2"],
                      "objective": "y1", "n_calls": 40, "seed": 0}]}
    r = Orchestrator(vocs, Evaluator(optical_bench), pipe).run()
    assert r["objectives"]["y1"] > 0.9
    # reported state must be consistent with a fresh recompute
    assert abs(optical_bench(r["state"])["y1"] - r["objectives"]["y1"]) < 1e-6


def test_global_until_syncs_state():
    # regression: when global `until` fires mid-stage, state must match objectives
    vocs = demo_vocs()
    pipe = {"until": "y1>=0.98", "flow": [
        {"stage": "find", "algorithm": "grid_scan", "variables": ["x1", "x2"],
         "objective": "y1", "n_per_axis": 5, "stop": {"target": "y1>0.2"}},
        {"stage": "bo", "algorithm": "bayesian", "variables": ["x1", "x2"],
         "objective": "y1", "n_calls": 40, "seed": 1}]}
    r = Orchestrator(vocs, Evaluator(optical_bench), pipe).run()
    assert abs(optical_bench(r["state"])["y1"] - r["objectives"]["y1"]) < 1e-6


def test_hardware_adapter_with_noise_and_averaging():
    vocs = demo_vocs()
    stage = SimulatedStage(vocs.initial_point())
    meter = SimulatedMeter(stage, optical_bench, noise=0.01, seed=0)
    ev = HardwareEvaluator(stage, meter, averages=5,
                           safety=SafetyLimits({"x1": (-3, 7), "x2": (-6, 4), "x3": (0, 1.5)}))
    r = Orchestrator(vocs, ev, TWO_PHASE_PIPELINE).run()
    assert r["objectives"]["y1"] > 0.95


def test_safety_limits_strict_and_clamp():
    stage = SimulatedStage({"x1": 0, "x2": 0, "x3": 0})
    meter = SimulatedMeter(stage, optical_bench)
    # strict -> raise
    ev = HardwareEvaluator(stage, meter, safety=SafetyLimits({"x1": (0, 1)}, strict=True))
    try:
        ev.evaluate({"x1": 5, "x2": 0, "x3": 0})
        assert False, "should have raised SafetyViolation"
    except SafetyViolation:
        pass
    # clamp -> move to the wall, no crash
    ev2 = HardwareEvaluator(stage, meter, safety=SafetyLimits({"x1": (0, 1)}))
    ev2.evaluate({"x1": 5, "x2": 0, "x3": 0})
    assert stage.pos["x1"] == 1


def test_sqlite_archive_resume_rollback():
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "run.db")
        vocs = demo_vocs()
        stage = SimulatedStage(vocs.initial_point())
        meter = SimulatedMeter(stage, optical_bench, noise=0.005, seed=0)
        store = SQLiteStore(db, run_id="r1")
        ev = HardwareEvaluator(stage, meter, averages=3, store=store)
        Orchestrator(vocs, ev, TWO_PHASE_PIPELINE).run()

        # archive persisted and reopenable
        store2 = SQLiteStore(db, run_id="r1")
        assert len(store2.history()) > 0
        pt, objs = store2.best("y1", "max")
        assert objs["y1"] > 0.9

        # resume from best archived point
        orch = Orchestrator(vocs, ev, TWO_PHASE_PIPELINE, start_point=pt)
        assert orch.state["x1"] == pt["x1"]

        # rollback drives the (wandered) stage back to best
        stage.move("x1", -2)
        back = rollback_to_best(ev, store2, "y1", "max")
        assert stage.pos["x1"] == back["x1"]
