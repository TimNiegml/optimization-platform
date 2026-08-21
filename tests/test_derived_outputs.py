import pytest

from optplat.engine import StageEngine
from optplat.evaluator import DerivedEvaluator, Evaluator
from optplat.vocs import Objective, VOCS, Variable
from optplat.sensitivity import SensitivitySpec, run_sensitivity


def derived_vocs():
    return VOCS(
        variables={"x1": Variable(low=-2, high=2)},
        objectives={
            "y1": Objective(),
            "y2": Objective(),
            "z1": Objective(expression="y1 + y2"),
            "z2": Objective(expression="max(y1, y2, z1) - min(y1, y2)"),
        },
    )


def test_derived_evaluator_expands_physical_dependencies_and_traces_values():
    base = Evaluator(lambda x: {"y1": x["x1"], "y2": 2 * x["x1"]},
                     costs={"y1": 1.0, "y2": 2.0})
    ev = DerivedEvaluator(base, {"z1": "y1+y2", "z2": "max(z1,y2)-min(y1,y2)"},
                          ["y1", "y2", "z1", "z2"])

    assert ev.evaluate({"x1": 2.0}, channels={"z2"}) == {"z2": 4.0}
    assert base.reads == {"y1": 1, "y2": 1}  # calculated channels are not instrument reads
    assert base.sim_seconds == pytest.approx(3.0)
    assert base.history[-1]["z2"] == pytest.approx(4.0)


def test_derived_output_can_be_an_optimization_objective():
    vocs = derived_vocs()
    engine = StageEngine(vocs, Evaluator(lambda x: {
        "y1": -(x["x1"] - 1.0) ** 2,
        "y2": -(x["x1"] - 1.0) ** 2,
    }))
    engine.prime()
    engine.run_stage({"algorithm": "coordinate_descent", "stage": "opt-z",
                      "variables": ["x1"], "objective": "z1",
                      "stop": {"max_iter": 100}})
    assert engine.state["x1"] == pytest.approx(1.0, abs=0.02)
    assert engine.last_y["z1"] == pytest.approx(0.0, abs=1e-3)


def test_derived_output_can_be_used_in_sensitivity_matrix():
    vocs = VOCS(
        variables={"x1": Variable(low=-2, high=2)},
        objectives={"y1": Objective(), "y2": Objective(),
                    "z1": Objective(expression="y1-y2")},
    )
    result = run_sensitivity(
        vocs, Evaluator(lambda x: {"y1": 3*x["x1"], "y2": x["x1"]}),
        SensitivitySpec(variables=["x1"], objectives=["z1"], step=0.5, n_points=5),
    )
    assert result["matrix"]["z1"]["x1"] == pytest.approx(2.0)


def test_derived_output_rejects_unknown_names_and_cycles_before_evaluation():
    base = Evaluator(lambda x: {"y1": 1.0})
    with pytest.raises(ValueError, match="unknown channels"):
        DerivedEvaluator(base, {"z1": "missing+1"}, ["y1", "z1"])
    with pytest.raises(ValueError, match="cyclic"):
        DerivedEvaluator(base, {"z1": "z2+1", "z2": "z1+1"},
                         ["y1", "z1", "z2"])


def test_derived_output_expression_dsl_rejects_code_execution():
    base = Evaluator(lambda x: {"y1": 1.0})
    with pytest.raises(ValueError, match="unknown channels"):
        DerivedEvaluator(base, {"z1": "__import__('os')"}, ["y1", "z1"])


def test_live_derived_value_refreshes_during_selective_reads():
    vocs = derived_vocs()
    engine = StageEngine(vocs, Evaluator(lambda x: {
        "y1": x["x1"], "y2": 10 + x["x1"],
    }))
    seen = []
    engine.on_eval = lambda x, y, stage, n: seen.append(dict(y))
    engine.prime()  # establishes y1, y2 and both derived outputs
    engine.evaluate({"x1": 1.5}, "only-y1", channels={"y1"})

    assert seen[-1]["y1"] == pytest.approx(1.5)
    assert seen[-1]["y2"] == pytest.approx(10.0)  # latest cached, not re-read
    assert seen[-1]["z1"] == pytest.approx(11.5)  # refreshed, not stale at 10
    assert seen[-1]["z2"] == pytest.approx(10.0)
