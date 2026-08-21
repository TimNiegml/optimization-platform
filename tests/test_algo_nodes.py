"""螺旋扫描 / 贝叶斯搜索范围与探索系数 / 每轴初始单纯形 / 任意变量个数 / 流式运行。

贯穿的一条主线：**场景不同，自变量个数就不同**——所有算子都不能把维度写死。
"""
import math

import pytest

from optplat.demo import demo_vocs, optical_bench
from optplat.evaluator import Evaluator
from optplat.generators import NelderMead, SpiralScan
from optplat.graph import GraphRunner
from optplat.registry import GROUPS, REGISTRY, algorithm_catalog
from optplat.vocs import VOCS, Objective, Variable


def _vocs(n_vars: int, n_objs: int = 1) -> VOCS:
    return VOCS(variables={f"x{i}": Variable(low=-5.0, high=5.0) for i in range(1, n_vars + 1)},
                objectives={f"y{j}": Objective() for j in range(1, n_objs + 1)})


def _sweep(gen, base):
    gen.set_base(base)
    pts = []
    while not gen.done:
        p = gen.ask()
        pts.append(p)
        gen.tell(p, 0.0)
    return pts


# ---------------- 螺旋扫描 ----------------
@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_spiral_works_for_any_number_of_variables(n):
    """场景决定变量个数，节点不能写死维度。"""
    v = _vocs(n)
    xs = [f"x{i}" for i in range(1, n + 1)]
    base = {x: 0.0 for x in xs}
    pts = _sweep(SpiralScan(v, xs, "y1", step=0.4, points_per_turn=6, turns=3), base)
    assert len(pts) == 18
    r = [math.dist([p[x] for x in xs], [0.0] * n) for p in pts]
    assert r[0] < r[-1]                                   # 从原点向外
    assert all(a <= b + 1e-9 for a, b in zip(r, r[1:]))   # 半径单调不减


def test_spiral_starts_at_the_origin_not_the_range_centre():
    """真实台架没有绝对坐标：螺旋必须绕【当前位置】展开。"""
    v = _vocs(2)
    base = {"x1": 3.0, "x2": -2.0}
    pts = _sweep(SpiralScan(v, ["x1", "x2"], "y1", step=0.2, points_per_turn=8, turns=1), base)
    for p in pts:                                          # 全部落在原点的小邻域内
        assert math.dist([p["x1"], p["x2"]], [3.0, -2.0]) <= 0.2 + 1e-9
    assert any(p["x1"] > 3.0 for p in pts) and any(p["x1"] < 3.0 for p in pts)


def test_spiral_2d_is_an_archimedean_spiral():
    """2 维是标准阿基米德螺旋：转满一圈半径正好长一个 step。"""
    v = _vocs(2)
    ppt = 12
    pts = _sweep(SpiralScan(v, ["x1", "x2"], "y1", step=0.5, points_per_turn=ppt, turns=2),
                 {"x1": 0.0, "x2": 0.0})
    r = [math.hypot(p["x1"], p["x2"]) for p in pts]
    assert r[ppt - 1] == pytest.approx(0.5, abs=1e-9)      # 第一圈末 = 1×step
    assert r[2 * ppt - 1] == pytest.approx(1.0, abs=1e-9)  # 第二圈末 = 2×step
    ang = [math.atan2(p["x2"], p["x1"]) for p in pts[:ppt]]
    assert len(set(round(a, 6) for a in ang)) == ppt       # 一圈内角度不重复


def test_spiral_per_axis_step_handles_different_scales():
    v = _vocs(2)
    pts = _sweep(SpiralScan(v, ["x1", "x2"], "y1", step=1.0, steps={"x2": 0.01},
                            points_per_turn=8, turns=1), {"x1": 0.0, "x2": 0.0})
    assert max(abs(p["x1"]) for p in pts) > 0.5            # x1 走得远
    assert max(abs(p["x2"]) for p in pts) < 0.02           # x2 按自己的小步距


def test_spiral_respects_bounds():
    v = VOCS(variables={"x1": Variable(low=0.0, high=1.0), "x2": Variable(low=0.0, high=1.0)},
             objectives={"y1": Objective()})
    pts = _sweep(SpiralScan(v, ["x1", "x2"], "y1", step=5.0, points_per_turn=6, turns=2),
                 {"x1": 0.5, "x2": 0.5})
    assert all(0.0 <= p[k] <= 1.0 for p in pts for k in ("x1", "x2"))


def test_spiral_finds_the_peak_in_a_graph_and_stops_at_threshold():
    graph = {"nodes": [
        {"id": "s", "type": "start"},
        {"id": "sp", "type": "algorithm", "data": {
            "algorithm": "spiral_scan", "variables": ["x1", "x2"], "objective": "y1",
            "step": 0.3, "points_per_turn": 10, "turns": 6, "stop": {"target": "y1>0.9"}}},
        {"id": "e", "type": "end"}],
        "edges": [{"source": "s", "target": "sp"}, {"source": "sp", "target": "e"}]}
    res = GraphRunner(demo_vocs(), Evaluator(optical_bench), graph,
                     start_point={"x1": 1.0, "x2": -2.0, "x3": 0.5}).run()
    assert res["objectives"]["y1"] > 0.9                   # 从偏离处螺旋出去找到了光


# ---------------- 贝叶斯：初始搜索范围 + 探索系数 ----------------
def test_bayes_span_frac_confines_search_around_the_origin():
    pytest.importorskip("optuna")
    from optplat.bayes import BayesianGenerator
    v = demo_vocs()
    g = BayesianGenerator(v, ["x1"], "y1", n_calls=15, seed=0, span_frac=0.2)
    pts = []
    g.set_base({"x1": 2.0, "x2": 0.0, "x3": 0.5})
    while not g.done:
        p = g.ask(); pts.append(p["x1"]); g.tell(p, 0.0)
    half = 0.5 * 0.2 * (v.variables["x1"].high - v.variables["x1"].low)
    assert min(pts) >= 2.0 - half - 1e-9 and max(pts) <= 2.0 + half + 1e-9


def test_bayes_span_frac_zero_searches_the_full_range():
    pytest.importorskip("optuna")
    from optplat.bayes import BayesianGenerator
    v = demo_vocs()
    g = BayesianGenerator(v, ["x1"], "y1", n_calls=25, seed=0, span_frac=0.0)
    g.set_base({"x1": 2.0, "x2": 0.0, "x3": 0.5})
    pts = []
    while not g.done:
        p = g.ask(); pts.append(p["x1"]); g.tell(p, 0.0)
    assert max(pts) - min(pts) > 0.5 * (v.variables["x1"].high - v.variables["x1"].low)


def test_bayes_explicit_axis_range_overrides_relative_window():
    pytest.importorskip("optuna")
    from optplat.bayes import BayesianGenerator
    v = demo_vocs()
    g = BayesianGenerator(v, ["x1"], "y1", n_calls=12, seed=0, span_frac=0.8,
                          search_ranges={"x1": [1.25, 1.75]})
    g.set_base({"x1": 4.0, "x2": 0.0, "x3": 0.5})
    pts = []
    while not g.done:
        p = g.ask(); pts.append(p["x1"]); g.tell(p, 0.0)
    assert all(1.25 <= x <= 1.75 for x in pts)


def test_bayes_exploration_knobs_are_accepted_and_run():
    pytest.importorskip("optuna")
    from optplat.bayes import BayesianGenerator
    g = BayesianGenerator(demo_vocs(), ["x1", "x2"], "y1", n_calls=12, seed=1,
                          n_startup=3, explore=0.4)
    assert g.n_startup == 3 and g.explore == pytest.approx(0.4)
    g.set_base({"x1": 2.0, "x2": 0.0, "x3": 0.5})
    n = 0
    while not g.done:
        p = g.ask(); g.tell(p, optical_bench({**{"x3": 0.5}, **p})["y1"]); n += 1
    assert n == 12 and g.best_x is not None


def test_bayes_params_reach_the_generator_through_the_node_spec():
    pytest.importorskip("optuna")
    gen = REGISTRY["bayesian"].builder(
        demo_vocs(), {"variables": ["x1"], "objective": "y1",
                      "span_frac": 0.3, "n_startup": 5, "explore": 0.25, "n_calls": 11})
    assert gen.span_frac == pytest.approx(0.3) and gen.n_startup == 5
    assert gen.explore == pytest.approx(0.25) and gen.n_calls == 11


# ---------------- 单纯形：每轴初始边长 ----------------
def test_nelder_mead_initial_simplex_is_per_axis_from_the_current_point():
    """x 从当前位置启动，初始单纯形边长可以逐轴给 (Δx1, Δx2, Δx3)。"""
    v = _vocs(3)
    base = {"x1": 1.0, "x2": 2.0, "x3": 3.0}
    deltas = {"x1": 0.1, "x2": 0.5, "x3": 2.0}
    g = NelderMead(v, ["x1", "x2", "x3"], "y1", init_steps=deltas)
    g.set_base(base)
    verts = [g.ask()]
    g.tell(verts[0], 0.0)
    for _ in range(3):
        p = g.ask(); verts.append(p); g.tell(p, 0.0)
    assert verts[0] == pytest.approx(base)                  # 第一个顶点 = 当前位置
    for i, ax in enumerate(["x1", "x2", "x3"], start=1):    # 其余顶点各沿一轴偏 Δ
        assert verts[i][ax] == pytest.approx(base[ax] + deltas[ax])
        for other in set(base) - {ax}:
            assert verts[i][other] == pytest.approx(base[other])


@pytest.mark.parametrize("n", [1, 2, 4])
def test_nelder_mead_handles_any_number_of_variables(n):
    v = _vocs(n)
    xs = [f"x{i}" for i in range(1, n + 1)]
    g = NelderMead(v, xs, "y1", init_step_frac=0.1)
    g.set_base({x: 0.0 for x in xs})
    for _ in range(n + 3):
        p = g.ask(); g.tell(p, -sum(val * val for val in p.values()))
    assert g.best_x is not None and set(g.best_x) == set(xs)


# ---------------- 节点分组 ----------------
def test_palette_groups_cover_every_algorithm():
    cat = algorithm_catalog()
    valid = {k for k, _, _ in GROUPS}
    assert {a["group"] for a in cat} <= valid
    by = {}
    for a in cat:
        by.setdefault(a["group"], []).append(a["name"])
    assert {"grid_scan", "line_scan", "spiral_scan"} <= set(by["find"])
    assert "bayesian" in by["coarse"]
    assert {"nelder_mead", "coordinate_descent"} <= set(by["fine"])
    assert {"damped_sensitivity", "sensitivity_scan"} <= set(by["solve"])
    assert all(a["group_label"] for a in cat)               # 画布靠它建分组标题


def test_spiral_joins_the_coarse_phase_of_autotune_automatically():
    from optplat.autotune import phase_algorithms
    assert "spiral_scan" in phase_algorithms()["coarse"]    # category=find-light → 自动入相


# ---------------- 流式运行（右栏实时状态的数据源）----------------
def test_stream_run_emits_every_point_then_the_same_result():
    pytest.importorskip("httpx")
    import json

    from fastapi.testclient import TestClient
    from optplat.api import app
    graph = {"nodes": [
        {"id": "s", "type": "start"},
        {"id": "a", "type": "algorithm", "data": {
            "algorithm": "spiral_scan", "variables": ["x1", "x2"], "objective": "y1",
            "step": 0.3, "points_per_turn": 6, "turns": 2}},
        {"id": "e", "type": "end"}],
        "edges": [{"source": "s", "target": "a"}, {"source": "a", "target": "e"}]}
    body = {"graph": graph, "delay": 0.0}
    client = TestClient(app)
    evs, done, start = [], None, None
    with client.stream("POST", "/run/graph/stream", json=body) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            d = json.loads(line[5:].strip())
            if d["type"] == "eval":
                evs.append(d)
            elif d["type"] == "done":
                done = d["result"]
            elif d["type"] == "start":
                start = d
    assert start and start["variables"] and start["objectives"]
    assert done is not None
    assert len(evs) == done["n_evals"]                     # 每次评估都推了一条
    assert [e["n"] for e in evs] == list(range(1, len(evs) + 1))
    last = evs[-1]
    assert set(last["x"]) <= set(start["variables"]) and last["y"]
    # 流式与非流式跑出来是同一个结果（同一个执行核，不是第二条路径）
    plain = client.post("/run/graph", json={"graph": graph}).json()
    assert plain["n_evals"] == done["n_evals"]
    assert plain["objectives"]["y1"] == pytest.approx(done["objectives"]["y1"])


def test_stream_reports_errors_instead_of_hanging():
    pytest.importorskip("httpx")
    import json

    from fastapi.testclient import TestClient
    from optplat.api import app
    bad = {"nodes": [{"id": "a", "type": "algorithm",
                      "data": {"algorithm": "no_such_algo", "variables": ["x1"],
                               "objective": "y1"}}], "edges": []}
    kinds = []
    with TestClient(app).stream("POST", "/run/graph/stream",
                                json={"graph": bad, "delay": 0.0}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                kinds.append(json.loads(line[5:].strip())["type"])
    assert "error" in kinds


def test_engine_on_eval_hook_does_not_change_the_run():
    """观察者钩子必须是纯旁观：挂上它，结果一模一样。"""
    graph = {"nodes": [
        {"id": "s", "type": "start"},
        {"id": "a", "type": "algorithm", "data": {
            "algorithm": "grid_scan", "variables": ["x1"], "objective": "y1",
            "n_per_axis": 5}},
        {"id": "e", "type": "end"}],
        "edges": [{"source": "s", "target": "a"}, {"source": "a", "target": "e"}]}
    plain = GraphRunner(demo_vocs(), Evaluator(optical_bench), graph).run()
    seen = []
    r2 = GraphRunner(demo_vocs(), Evaluator(optical_bench), graph)
    r2.engine.on_eval = lambda x, y, stage, n: seen.append(n)
    hooked = r2.run()
    assert plain["n_evals"] == hooked["n_evals"] == len(seen)
    assert plain["state"] == pytest.approx(hooked["state"])
    assert plain["objectives"] == pytest.approx(hooked["objectives"])
