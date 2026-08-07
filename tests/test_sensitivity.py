"""灵敏度采集：单轴扫描 → 曲线 / 拟合 / ∂y/∂x 矩阵。

覆盖三件事：矩阵测得准（对已知真值台架）、线性范围是实测出来的（非线性台架上会收窄）、
以及它作为流程节点时会把操作点送回原点。
"""
import math

import pytest

from optplat.demo import demo_vocs, linear_sens_bench, nonlinear_sens_bench
from optplat.evaluator import Evaluator
from optplat.graph import GraphRunner
from optplat.hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage
from optplat.sensitivity import SensitivitySpec, fit_curve, run_sensitivity

# linear_sens_bench 的解析真值：y1=0.8x1+0.3x2+…, y2=0.2x1-0.6x2+…
TRUE_S = {"y1": {"x1": 0.8, "x2": 0.3}, "y2": {"x1": 0.2, "x2": -0.6}}


def _run(bench=linear_sens_bench, **kw):
    vocs = demo_vocs()
    spec = SensitivitySpec(variables=["x1", "x2"], objectives=["y1", "y2"],
                           step=kw.pop("step", 0.4), n_points=kw.pop("n_points", 7), **kw)
    return run_sensitivity(vocs, Evaluator(bench), spec)


# ---------------- 矩阵 ----------------
def test_matrix_recovers_known_sensitivities():
    out = _run()
    for o, row in TRUE_S.items():
        for v, truth in row.items():
            assert out["matrix"][o][v] == pytest.approx(truth, abs=1e-6), f"∂{o}/∂{v}"
    assert out["fit"] == "linear"


def test_matrix_shape_feeds_damped_sensitivity_solver():
    """矩阵形状必须能直接喂给 damped_sensitivity —— 这是这个功能的主要下游。"""
    out = _run()
    graph = {"nodes": [
        {"id": "s", "type": "start"},
        {"id": "solve", "type": "algorithm", "data": {
            "algorithm": "damped_sensitivity", "variables": ["x1", "x2"], "objective": "y1",
            "targets": {"y1": 2.0, "y2": 0.0}, "sensitivity": out["matrix"],
            "damping": 0.8, "max_solves": 30}},
        {"id": "e", "type": "end"}],
        "edges": [{"source": "s", "target": "solve"}, {"source": "solve", "target": "e"}]}
    res = GraphRunner(demo_vocs(), Evaluator(linear_sens_bench), graph).run()
    assert abs(res["objectives"]["y1"] - 2.0) < 1e-2      # 用【实测】矩阵解到目标
    assert abs(res["objectives"]["y2"] - 0.0) < 1e-2


def test_selective_read_only_requested_objectives():
    vocs = demo_vocs()
    ev = Evaluator(linear_sens_bench)
    run_sensitivity(vocs, ev, SensitivitySpec(variables=["x1"], objectives=["y1"],
                                              step=0.3, n_points=5))
    assert ev.reads.get("y1", 0) > 0
    assert ev.reads.get("y2", 0) == 0          # 没选的通道一次都不读


def test_origin_measured_once_and_reused():
    """reuse_origin=True 时原点只测一次：1 + n_axes×(n_points−1) + 收尾回原点。"""
    vocs = demo_vocs()
    ev = Evaluator(linear_sens_bench)
    out = run_sensitivity(vocs, ev, SensitivitySpec(
        variables=["x1", "x2"], objectives=["y1"], step=0.4, n_points=7))
    assert out["n_evals"] == 1 + 2 * 6 + 1


# ---------------- 线性范围 ----------------
def test_linear_range_full_on_a_straight_line():
    out = _run()
    f = out["origins"][0]["fits"]["y1"]["x1"]
    assert f["linear_full"] is True
    assert f["r2"] == pytest.approx(1.0, abs=1e-9)


def test_linear_range_narrows_where_the_bench_bends():
    """非线性台架上，扫得越宽线性范围越是真子集 —— 这正是用户要看的东西。"""
    vocs = demo_vocs()
    spec = SensitivitySpec(variables=["x1"], objectives=["y1"], step=0.5, n_points=9,
                           origins=[{"x1": 4.0, "x2": 1.0}], fit="quadratic")
    out = run_sensitivity(vocs, Evaluator(nonlinear_sens_bench), spec)
    f = out["origins"][0]["fits"]["y1"]["x1"]
    lo, hi = f["linear_range"]
    assert not f["linear_full"]
    assert 2.0 < lo < 4.0 < hi < 6.0          # 比扫描全程 [2,6] 窄，且含原点
    assert f["curvature"] != 0.0              # 二次拟合确实抓到了弯曲


def test_flat_channel_reports_no_sensitivity_instead_of_dividing_by_zero():
    out = _run(bench=lambda x: {"y1": 1.0, "y2": 1.0})
    f = out["origins"][0]["fits"]["y1"]["x1"]
    assert f["linear_range"] is None and "note" in f


# ---------------- 多原点 ----------------
def test_multi_origin_exposes_drift_of_the_sensitivity():
    vocs = demo_vocs()
    spec = SensitivitySpec(variables=["x1"], objectives=["y1"], step=0.5, n_points=9,
                           origins=[{"x1": 2.0, "x2": 0.0}, {"x1": 4.0, "x2": 1.0}])
    out = run_sensitivity(vocs, Evaluator(nonlinear_sens_bench), spec)
    a = out["origins"][0]["matrix"]["y1"]["x1"]
    b = out["origins"][1]["matrix"]["y1"]["x1"]
    assert abs(a - b) > 0.1                              # 工作点不同，灵敏度不同
    assert out["matrix_std"]["y1"]["x1"] > 0
    assert any("漂移" in s for s in out["summary"])


def test_identical_origins_report_zero_drift_not_float_noise():
    vocs = demo_vocs()
    spec = SensitivitySpec(variables=["x1"], objectives=["y1"], step=0.4, n_points=5,
                           origins=[{"x1": 2.0}, {"x1": 2.0}])
    out = run_sensitivity(vocs, Evaluator(linear_sens_bench), spec)
    assert out["matrix_std"]["y1"]["x1"] == 0.0          # 不是 3e-16


# ---------------- 噪声 / 越界 ----------------
def test_averaging_tames_noise_so_the_slope_stays_close():
    vocs = demo_vocs()
    stage = SimulatedStage(vocs.initial_point())
    ev = HardwareEvaluator(stage, SimulatedMeter(stage, linear_sens_bench, noise=0.05, seed=3),
                           averages=8)
    out = run_sensitivity(vocs, ev, SensitivitySpec(
        variables=["x1"], objectives=["y1"], step=0.5, n_points=9))
    assert out["matrix"]["y1"]["x1"] == pytest.approx(0.8, abs=0.05)


def test_out_of_range_points_are_clipped_and_warned():
    vocs = demo_vocs()
    hi = vocs.variables["x1"].high
    out = run_sensitivity(vocs, Evaluator(linear_sens_bench), SensitivitySpec(
        variables=["x1"], objectives=["y1"], step=50.0, n_points=5,
        origins=[{"x1": hi}]))
    assert out["warnings"] and "超出量程" in out["warnings"][0]


def test_unknown_variable_is_rejected():
    with pytest.raises(ValueError):
        run_sensitivity(demo_vocs(), Evaluator(linear_sens_bench),
                        SensitivitySpec(variables=["nope"], objectives=["y1"]))


# ---------------- 作为流程节点 ----------------
def test_scan_node_returns_operating_point_to_the_origin():
    """采集完不能把机构留在最后一个扫描点 —— 执行核应把操作点送回原点。"""
    vocs = demo_vocs()
    start = {"x1": 2.0, "x2": -1.0, "x3": 0.5}
    graph = {"nodes": [
        {"id": "s", "type": "start"},
        {"id": "sens", "type": "algorithm", "data": {
            "algorithm": "sensitivity_scan", "variables": ["x1", "x2"], "objective": "y1",
            "step": 0.4, "n_points": 5}},
        {"id": "e", "type": "end"}],
        "edges": [{"source": "s", "target": "sens"}, {"source": "sens", "target": "e"}]}
    res = GraphRunner(vocs, Evaluator(linear_sens_bench), graph, start_point=start).run()
    assert res["state"]["x1"] == pytest.approx(2.0) and res["state"]["x2"] == pytest.approx(-1.0)
    assert "灵敏度矩阵" in res["fits"]["sensitivity_scan"]   # stage 名默认取算法名


def test_scan_node_matrix_matches_the_standalone_acquisition():
    vocs = demo_vocs()
    from optplat.generators import SensitivityScan
    gen = SensitivityScan(vocs, ["x1", "x2"], "y1", step=0.4, n_points=5)
    gen.set_base({"x1": 2.0, "x2": -1.0, "x3": 0.5})
    while not gen.done:
        x = gen.ask()
        gen.observe(x, linear_sens_bench(x))
    for o, row in TRUE_S.items():
        for v, truth in row.items():
            assert gen.matrix[o][v] == pytest.approx(truth, abs=1e-6)


# ---------------- 拟合函数本身 ----------------
def test_fit_curve_quadratic_slope_is_the_derivative_at_the_origin():
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    ys = [x * x for x in xs]                   # y=x² → dy/dx|₂ = 4
    f = fit_curve(xs, ys, x0=2.0, order=2)
    assert f["slope"] == pytest.approx(4.0, abs=1e-6)
    assert f["curvature"] == pytest.approx(2.0, abs=1e-6)


def test_fit_curve_handles_two_points():
    f = fit_curve([0.0, 1.0], [0.0, 3.0], x0=0.0, order=2)
    assert f["slope"] == pytest.approx(3.0)    # 点数不够二次，自动退回一次


# ---------------- API ----------------
def test_api_sensitivity_endpoint():
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from optplat.api import app
    r = TestClient(app).post("/sensitivity", json={
        "variables": ["x1", "x2"], "objectives": ["y1", "y2"],
        "step": 0.4, "n_points": 7,
        "evaluator": {"mode": "function", "bench": "linear_sens"}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["matrix"]["y1"]["x1"] == pytest.approx(0.8, abs=1e-6)
    assert d["origins"][0]["curves"]["x1"]["xs"]          # 曲线点给到前端作图
    assert not math.isnan(d["origins"][0]["curves"]["x1"]["y"]["y1"][0])


def test_mcp_measure_sensitivity_tool():
    from optplat import mcp_server as m
    out = m.measure_sensitivity(["x1", "x2"], ["y1", "y2"], step=0.4, n_points=5,
                                bench="linear_sens")
    assert out["matrix"]["y2"]["x2"] == pytest.approx(-0.6, abs=1e-6)
    assert out["linearity"]["y1"]["x1"]["r2"] == pytest.approx(1.0, abs=1e-6)
