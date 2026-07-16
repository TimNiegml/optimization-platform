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


def test_gradient_ascent_finds_peak():
    # PI 'lightning'-style measured-gradient ascent climbs the y1 lobe
    r = _run(_stage("gradient_ascent", ["x1", "x2"], "y1"))
    assert r["objectives"]["y1"] > 0.95
    assert abs(r["state"]["x1"] - 2.0) < 0.4
    assert abs(r["state"]["x2"] + 1.0) < 0.4


def test_surrogate_fit_exposes_formula():
    from optplat.generators import SurrogateFit
    g = SurrogateFit(demo_vocs(), ["x3"], "y2", model="quadratic", n_samples=5)
    g.set_base({"x3": 0.6})
    for _ in range(12):
        x = g.ask()
        g.tell(x, -((x["x3"] - 0.6) ** 2))     # concave parabola, peak at 0.6
        if g.done:
            break
    assert g.fit_info and "峰@" in g.fit_info    # fitted-formula summary returned


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


def test_parametric_fit_with_pinned_params():
    # 公式法 / 非标拟合: pin known parameters, fit only the rest.
    pre = {"stage": "pre", "algorithm": "nelder_mead", "variables": ["x1", "x2"],
           "objective": "y1", "stop": {"max_iter": 100}}
    # (a) custom expression with a known optical spot width sigma pinned
    custom = "amp * exp(-((x - center)**2) / (2 * sigma**2)) + offset"
    r = _run({"flow": [pre, {
        "stage": "nonstd", "algorithm": "parametric_fit", "variables": ["x3"],
        "objective": "y2", "model": custom, "fixed": {"sigma": 0.25},
        "hints": {"center": {"value": 0.5, "min": 0, "max": 1.5},
                  "amp": {"value": 1.0}, "offset": {"value": 0.0}},
        "n_samples": 4, "r2_gate": 0.0}]})
    assert r["objectives"]["y2"] > 0.98
    assert abs(r["state"]["x3"] - 0.6) < 0.05
    # (b) builtin gaussian with the vertex/center pinned
    r = _run({"flow": [pre, {
        "stage": "pin", "algorithm": "parametric_fit", "variables": ["x3"],
        "objective": "y2", "model": "gaussian", "fixed": {"center": 0.6},
        "n_samples": 4, "r2_gate": 0.0}]})
    assert r["objectives"]["y2"] > 0.98


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
