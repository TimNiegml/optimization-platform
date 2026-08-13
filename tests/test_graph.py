"""Graph runtime tests: node+edge JSON runs directly, branches, bounded loops,
and custom-algorithm plug-in via the registry."""
import random

import numpy as np
import pytest

from optplat import Evaluator, Generator, ObjectiveValueType
from optplat.demo import TWO_PHASE_GRAPH, demo_vocs, optical_bench
from optplat.graph import GraphRunner, to_mermaid
from optplat.hardware import HardwareEvaluator, SimulatedStage
from optplat.registry import AlgorithmSpec, algorithm_catalog, register_algorithm
from optplat.transforms import transform_value


def _run(graph, **kw):
    return GraphRunner(demo_vocs(), Evaluator(optical_bench), graph, **kw).run()


def test_graph_two_phase_converges():
    r = _run(TWO_PHASE_GRAPH)
    assert r["objectives"]["y1"] > 0.98
    assert r["objectives"]["y2"] > 0.95
    # state consistent with a fresh recompute
    assert abs(optical_bench(r["state"])["y1"] - r["objectives"]["y1"]) < 1e-6


def test_conditional_edge_branches():
    # start -> gate; gate branches to A (raise y1) unless already high
    graph = {
        "nodes": [
            {"id": "gate", "type": "algorithm", "data": {
                "algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
                "n_per_axis": 5, "stop": {"target": "y1 > 0.15"}}},
            {"id": "refine", "type": "algorithm", "data": {
                "algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"source": "gate", "target": "refine", "condition": "y1 < 0.9"},
            {"source": "gate", "target": "end"},
        ],
    }
    r = _run(graph)
    assert r["objectives"]["y1"] > 0.9        # branch to refine was taken


def test_loop_is_bounded_by_max_visits():
    # a self-looping node whose condition never lets it exit -> must stop at max_visits
    graph = {
        "nodes": [
            {"id": "n", "type": "algorithm", "max_visits": 3, "data": {
                "algorithm": "coordinate_descent", "variables": ["x1"], "objective": "y1",
                "stop": {"max_iter": 5}}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"source": "n", "target": "n", "condition": "y1 < 2"},   # y1<=1 always -> loops
            {"source": "n", "target": "end"},
        ],
    }
    r = _run(graph)
    assert any("max_visits" in e for e in r["events"])   # guardrail fired, no infinite loop


def test_custom_algorithm_plugs_in():
    class RandomSearch(Generator):
        def __init__(self, vocs, variables, objective, n=40, seed=0):
            super().__init__(vocs, variables, objective)
            self.n = n; self._i = 0; self._rng = random.Random(seed)

        def ask(self):
            return {v: self._rng.uniform(self.vocs.variables[v].low,
                                         self.vocs.variables[v].high)
                    for v in self.variables}

        def tell(self, x, score):
            self._record(x, score); self._i += 1
            if self._i >= self.n:
                self.done = True

    register_algorithm(AlgorithmSpec(
        "random_search_test", "custom", False,
        lambda v, s: RandomSearch(v, s["variables"], s["objective"], n=s.get("n", 40)),
        params={"n": {"type": "int", "default": 40}}))

    assert any(a["name"] == "random_search_test" for a in algorithm_catalog())
    graph = {"nodes": [{"id": "s", "type": "algorithm", "data": {
        "algorithm": "random_search_test", "variables": ["x1", "x2"],
        "objective": "y1", "n": 60}}], "edges": []}
    r = _run(graph)
    assert r["objectives"]["y1"] > 0.5        # found the lobe without any engine change


def test_node_objective_mode_override():
    # y1 peaks at (x1,x2)=(2,-1). A node that *minimises* y1 must drive it low,
    # the opposite of the VOCS default (maximize) — proving the per-node override.
    graph = {"nodes": [{"id": "n", "type": "algorithm", "data": {
        "algorithm": "coordinate_descent", "variables": ["x1", "x2"],
        "objective": "y1", "objective_mode": "minimize",
        "stop": {"max_iter": 200}}}], "edges": []}
    r = _run(graph)
    assert r["objectives"]["y1"] < 0.05          # pushed away from the peak


def test_node_objective_target_mode():
    # drive y1 to a target value (标定): |y1 - 0.5| -> 0
    graph = {"nodes": [{"id": "n", "type": "algorithm", "data": {
        "algorithm": "coordinate_descent", "variables": ["x1", "x2"],
        "objective": "y1", "objective_mode": "target", "objective_target": 0.5,
        "stop": {"max_iter": 200}}}], "edges": []}
    r = _run(graph)
    assert abs(r["objectives"]["y1"] - 0.5) < 0.05


def test_composite_weighted_objective():
    # one operator optimises a weighted blend of y1 and y2 over all three vars
    graph = {"nodes": [{"id": "n", "type": "algorithm", "data": {
        "algorithm": "nelder_mead", "variables": ["x1", "x2", "x3"],
        "objective": "y1", "objective_weights": {"y1": 0.5, "y2": 0.5},
        "stop": {"max_iter": 400}}}], "edges": []}
    r = _run(graph)
    assert r["objectives"]["y1"] > 0.95 and r["objectives"]["y2"] > 0.9   # joint optimum
    assert any("加权组合" in e for e in r["events"])


def test_selective_channel_reads_and_cost():
    # a y1-only stage (no keep, no until) must NOT read y2 during the stage;
    # y2 is only measured once, by the initial prime(). Costs accumulate per read.
    ev = Evaluator(optical_bench, costs={"y1": 1.0, "y2": 2.0})
    graph = {"nodes": [{"id": "n", "type": "algorithm", "data": {
        "algorithm": "coordinate_descent", "variables": ["x1", "x2"],
        "objective": "y1", "stop": {"max_iter": 100}}}], "edges": []}
    r = GraphRunner(demo_vocs(), ev, graph).run()
    assert r["reads"]["y2"] == 1                       # only the prime() read
    assert r["reads"]["y1"] == r["n_evals"]            # y1 read every evaluation
    assert r["sim_seconds"] == r["reads"]["y1"] * 1.0 + r["reads"]["y2"] * 2.0


def test_until_forces_extra_channel_reads():
    # a global until referencing y2 forces y2 to be measured every step, even
    # though the stage optimises y1 — you must read what you check.
    ev = Evaluator(optical_bench, costs={"y1": 1.0, "y2": 2.0})
    graph = {"until": "y1>=0.99 and y2>=0.99",
             "nodes": [{"id": "n", "type": "algorithm", "data": {
                 "algorithm": "coordinate_descent", "variables": ["x1", "x2"],
                 "objective": "y1", "stop": {"max_iter": 60}}}], "edges": []}
    r = GraphRunner(demo_vocs(), ev, graph).run()
    assert r["reads"]["y2"] > 1                        # until makes y2 be read too


def test_fit_formula_reported():
    # from the initial point (x1,x2)=(2,-1) y1=1, so y2 is a clean gaussian in x3
    graph = {"nodes": [{"id": "n", "type": "algorithm", "data": {
        "algorithm": "gaussian_fit", "variables": ["x3"], "objective": "y2",
        "n_samples": 7}}], "edges": []}
    r = _run(graph)
    assert r["fits"] and any("峰@" in v for v in r["fits"].values())


def test_catalog_has_chinese_labels():
    cat = {a["name"]: a for a in algorithm_catalog()}
    assert cat["grid_scan"]["label"] == "网格扫描"
    assert cat["bayesian"]["label"] and cat["bayesian"]["desc"]


def test_to_mermaid_renders():
    m = to_mermaid(TWO_PHASE_GRAPH)
    assert "flowchart" in m and "find_light" in m and "-->" in m


def test_observer_between_nodes_captures_fresh_scalar_values():
    graph = {"nodes": [
        {"id": "a", "type": "algorithm", "data": {"algorithm": "coordinate_descent",
         "variables": ["x1"], "objective": "y1", "stop": {"max_iter": 10}}},
        {"id": "eye", "type": "observer", "data": {"kind": "scalar",
         "label": "节点1后", "channels": ["y1", "y2"]}},
        {"id": "b", "type": "algorithm", "data": {"algorithm": "coordinate_descent",
         "variables": ["x2"], "objective": "y1", "stop": {"max_iter": 10}}},
    ], "edges": [{"source": "a", "target": "eye"}, {"source": "eye", "target": "b"}]}
    result = _run(graph)
    snap = result["observations"]["eye"][0]
    assert snap["kind"] == "scalar" and set(snap["values"]) == {"y1", "y2"}
    assert any("节点1后" in event for event in result["events"])


def test_matrix_observer_preserves_matrix_for_table_and_feature_plot():
    vocs = demo_vocs()
    vocs.objectives["matrix"] = vocs.objectives["y1"].model_copy()
    ev = Evaluator(lambda x: {**optical_bench(x), "matrix": np.array([[1, 2, 3], [4, 5, 6]])})
    graph = {"nodes": [{"id": "eye", "type": "observer", "data": {
        "kind": "matrix_plot", "channels": ["matrix"]}}], "edges": []}
    result = GraphRunner(vocs, ev, graph).run()
    assert result["observations"]["eye"][0]["values"]["matrix"] == [[1, 2, 3], [4, 5, 6]]
    assert result["reads"]["matrix"] == 2  # initial acquisition + observer acquisition


def test_observer_rejects_unknown_channels():
    graph = {"nodes": [{"id": "eye", "type": "observer",
                        "data": {"channels": ["missing"]}}], "edges": []}
    try:
        _run(graph)
        assert False, "unknown observer channel should fail"
    except ValueError as exc:
        assert "unknown channels" in str(exc)


def test_hardware_observer_averages_matrix_channels_elementwise():
    class Meter:
        def __init__(self): self.i = 0
        def read(self):
            self.i += 1
            return {"matrix": [[self.i, 2*self.i], [3*self.i, 4*self.i]]}

    vocs = demo_vocs()
    vocs.objectives = {"matrix": vocs.objectives["y1"].model_copy()}
    ev = HardwareEvaluator(SimulatedStage(vocs.initial_point()), Meter(), averages=2)
    result = GraphRunner(vocs, ev, {"nodes": [{"id": "eye", "type": "observer",
        "data": {"kind": "matrix", "channels": ["matrix"]}}], "edges": []}).run()
    # prime consumes reads 1/2, observer consumes 3/4 -> element-wise mean 3.5 multiples
    assert result["observations"]["eye"][0]["values"]["matrix"] == [[3.5, 7.0], [10.5, 14.0]]


def test_matrix_objective_is_accepted_but_not_directly_optimized():
    vocs = demo_vocs()
    vocs.objectives["image"] = vocs.objectives["y1"].model_copy(
        update={"value_type": ObjectiveValueType.MATRIX})
    ev = Evaluator(lambda x: {**optical_bench(x), "image": [[1, 2], [3, 4]]})
    observed = GraphRunner(vocs, ev, {"nodes": [{"id": "eye", "type": "observer",
        "data": {"kind": "matrix", "channels": ["image"]}}], "edges": []}).run()
    assert observed["objectives"]["image"] == [[1, 2], [3, 4]]

    graph = {"nodes": [{"id": "bad", "type": "algorithm", "data": {
        "algorithm": "coordinate_descent", "variables": ["x1"], "objective": "image"}}],
        "edges": []}
    with pytest.raises(ValueError, match="cannot be optimized directly"):
        GraphRunner(vocs, ev, graph).run()


def test_matrix_policy_maps_model_vector_to_selected_axes():
    vocs = demo_vocs()
    vocs.objectives["image"] = vocs.objectives["y1"].model_copy(
        update={"value_type": ObjectiveValueType.MATRIX})
    ev = Evaluator(lambda x: {**optical_bench(x), "image": [[1.0, 2.0], [3.0, 4.0]]})
    graph = {"nodes": [{"id": "policy", "type": "algorithm", "data": {
        "algorithm": "matrix_policy", "variables": ["x1", "x2"], "objective": "y1",
        "input_channel": "image", "feature_mode": "row_mean",
        "weights": [[1.0, 0.0], [0.0, -0.5]], "bias": [0.5, 0.5],
        "output_map": {"x1": 1, "x2": 0}, "action_mode": "absolute"}}], "edges": []}
    result = GraphRunner(vocs, ev, graph).run()
    # row means=[1.5,3.5], model output=[2.0,-1.25], explicit cross mapping
    assert result["state"]["x1"] == pytest.approx(-1.25)
    assert result["state"]["x2"] == pytest.approx(2.0)
    assert "x1=output[1]" in result["fits"]["matrix_policy"]


def test_matrix_policy_delta_mode_is_safety_clipped():
    vocs = demo_vocs()
    vocs.objectives["image"] = vocs.objectives["y1"].model_copy(
        update={"value_type": ObjectiveValueType.MATRIX})
    ev = Evaluator(lambda x: {**optical_bench(x), "image": [[100.0]]})
    graph = {"nodes": [{"id": "policy", "type": "algorithm", "data": {
        "algorithm": "matrix_policy", "variables": ["x1"], "objective": "y1",
        "input_channel": "image", "output_map": {"x1": 0}, "action_mode": "delta"}}],
        "edges": []}
    result = GraphRunner(vocs, ev, graph).run()
    assert result["state"]["x1"] == vocs.variables["x1"].high


def test_explicit_for_loop_executes_body_exact_number_of_times():
    graph = {"nodes": [
        {"id": "loop", "type": "for_loop", "data": {"iterations": 3}},
        {"id": "body", "type": "observer", "data": {"channels": ["y1"]}},
        {"id": "end", "type": "end"}],
        "edges": [
            {"source": "loop", "target": "body", "role": "body"},
            {"source": "body", "target": "loop"},
            {"source": "loop", "target": "end", "role": "exit"}]}
    result = _run(graph)
    assert len(result["observations"]["body"]) == 3
    assert result["n_evals"] == 4  # prime + three loop-body acquisitions


def test_for_loop_requires_explicit_body_and_exit_edges():
    graph = {"nodes": [{"id": "loop", "type": "for_loop", "data": {"iterations": 2}}],
             "edges": []}
    with pytest.raises(ValueError, match="needs one 'body' edge"):
        _run(graph)


def test_data_transform_matrix_to_scalar_can_drive_optimizer():
    vocs = demo_vocs()
    vocs.objectives["image"] = vocs.objectives["y1"].model_copy(
        update={"value_type": ObjectiveValueType.MATRIX})
    ev = Evaluator(lambda x: {**optical_bench(x), "image": [[x["x1"]], [x["x1"]]]})
    graph = {"nodes": [
        {"id": "feature", "type": "data_transform", "data": {
            "input": "image", "output": "image_mean", "operation": "mean"}},
        {"id": "opt", "type": "algorithm", "data": {
            "algorithm": "coordinate_descent", "variables": ["x1"],
            "objective": "image_mean", "stop": {"max_iter": 20}}}],
        "edges": [{"source": "feature", "target": "opt"}]}
    result = GraphRunner(vocs, ev, graph).run()
    assert result["state"]["x1"] > 3.5
    assert "image_mean" in result["objectives"]
    assert result["reads"]["image"] > 0
    assert "image_mean" not in result["reads"]


def test_data_transform_supports_roi_and_rejects_unknown_input():
    vocs = demo_vocs()
    vocs.objectives["image"] = vocs.objectives["y1"].model_copy(
        update={"value_type": ObjectiveValueType.MATRIX})
    ev = Evaluator(lambda x: {**optical_bench(x), "image": np.arange(16).reshape(4, 4)})
    graph = {"nodes": [{"id": "crop", "type": "data_transform", "data": {
        "input": "image", "output": "roi", "operation": "roi",
        "params": {"row_start": 1, "row_end": 3, "column_start": 1, "column_end": 4}}}],
        "edges": []}
    result = GraphRunner(vocs, ev, graph).run()
    assert result["objectives"]["roi"] == [[5.0, 6.0, 7.0], [9.0, 10.0, 11.0]]

    bad = {"nodes": [{"id": "bad", "type": "data_transform", "data": {
        "input": "missing", "output": "z", "operation": "mean"}}], "edges": []}
    with pytest.raises(ValueError, match="unknown input"):
        GraphRunner(vocs, ev, bad)


def test_element_transform_selects_vector_or_matrix_scalar():
    assert transform_value([10, 20, 30], "element", {"indices": [1]}) == 20.0
    assert transform_value([[1, 2], [3, 4]], "element", {"indices": [1, 0]}) == 3.0
    with pytest.raises(ValueError, match="requires 2 indices"):
        transform_value([[1, 2]], "element", {"indices": [0]})
    with pytest.raises(ValueError, match="outside input shape"):
        transform_value([1, 2], "element", {"indices": [5]})


def test_vector_channel_requires_scalar_element_before_optimization():
    vocs = demo_vocs()
    vocs.objectives["spectrum"] = vocs.objectives["y1"].model_copy(
        update={"value_type": ObjectiveValueType.VECTOR})
    ev = Evaluator(lambda x: {**optical_bench(x), "spectrum": [x["x1"], -x["x1"]]})
    direct = {"nodes": [{"id": "bad", "type": "algorithm", "data": {
        "algorithm": "coordinate_descent", "variables": ["x1"], "objective": "spectrum"}}],
        "edges": []}
    with pytest.raises(ValueError, match="array channels cannot be optimized directly"):
        GraphRunner(vocs, ev, direct).run()

    selected = {"nodes": [
        {"id": "pick", "type": "data_transform", "data": {
            "input": "spectrum", "output": "spectrum_0", "operation": "element",
            "params": {"indices": [0]}}},
        {"id": "opt", "type": "algorithm", "data": {
            "algorithm": "coordinate_descent", "variables": ["x1"],
            "objective": "spectrum_0", "stop": {"max_iter": 20}}}],
        "edges": [{"source": "pick", "target": "opt"}]}
    assert GraphRunner(vocs, ev, selected).run()["state"]["x1"] > 3.5
