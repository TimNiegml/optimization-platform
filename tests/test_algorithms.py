"""Regression smoke tests: every algorithm reaches the known optimum, and the
two declarative pipelines run end-to-end. Run: python -m pytest -q
"""
import yaml

from optplat import Evaluator, Orchestrator
from optplat.demo import (
    DEMO_PIPELINE,
    TWO_PHASE_PIPELINE,
    demo_vocs,
    optical_bench,
)


def _run(pipeline):
    return Orchestrator(demo_vocs(), Evaluator(optical_bench), pipeline).run()


def _stage(algo, variables, objective, **extra):
    return {"flow": [{"stage": "t", "algorithm": algo, "variables": variables,
                      "objective": objective, "stop": {"max_iter": 300}, **extra}]}


def test_local_optimizers_find_y1_peak():
    # y1 peaks at (x1, x2) = (2, -1)
    for algo in ("grid_scan", "coordinate_descent", "nelder_mead"):
        r = _run(_stage(algo, ["x1", "x2"], "y1"))
        assert r["objectives"]["y1"] > 0.98, algo
        assert abs(r["state"]["x1"] - 2.0) < 0.2, algo
        assert abs(r["state"]["x2"] + 1.0) < 0.2, algo


def test_single_var_methods_optimize_y2():
    # pre-position x1,x2, then optimise y2 over x3 (peaks near x3=0.6)
    for algo, extra in (("quadratic_fit", {"r2_gate": 0.0}),
                        ("gaussian_fit", {"r2_gate": 0.0}),
                        ("formula", {})):
        pipe = {"flow": [
            {"stage": "pre", "algorithm": "nelder_mead", "variables": ["x1", "x2"],
             "objective": "y1", "stop": {"max_iter": 100}},
            {"stage": "opt", "algorithm": algo, "variables": ["x3"],
             "objective": "y2", **extra}]}
        r = _run(pipe)
        assert r["objectives"]["y2"] > 0.95, algo


def test_two_phase_pipeline_converges():
    r = _run(TWO_PHASE_PIPELINE)
    assert r["objectives"]["y1"] > 0.98
    assert r["objectives"]["y2"] > 0.95


def test_loop_pipeline_converges():
    r = _run(DEMO_PIPELINE)
    assert r["objectives"]["y1"] > 0.95


def test_yaml_pipelines_load_and_run():
    for path in ("find_light_example.yaml", "pipeline_example.yaml"):
        with open(path) as fh:
            pipeline = yaml.safe_load(fh)
        r = _run(pipeline)
        assert r["objectives"]["y1"] > 0.9
