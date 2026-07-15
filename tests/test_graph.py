"""Graph runtime tests: node+edge JSON runs directly, branches, bounded loops,
and custom-algorithm plug-in via the registry."""
import random

from optplat import Evaluator, Generator
from optplat.demo import TWO_PHASE_GRAPH, demo_vocs, optical_bench
from optplat.graph import GraphRunner, to_mermaid
from optplat.registry import AlgorithmSpec, algorithm_catalog, register_algorithm


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


def test_catalog_has_chinese_labels():
    cat = {a["name"]: a for a in algorithm_catalog()}
    assert cat["grid_scan"]["label"] == "网格扫描"
    assert cat["bayesian"]["label"] and cat["bayesian"]["desc"]


def test_to_mermaid_renders():
    m = to_mermaid(TWO_PHASE_GRAPH)
    assert "flowchart" in m and "find_light" in m and "-->" in m
