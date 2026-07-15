"""Interactive demo console for the optimization platform.

    streamlit run app.py

Left sidebar = the knobs a user turns; main area = problem, pipeline, and live
results. Exercises the whole stack: 8 algorithms, function vs simulated-hardware
evaluation (noise / averaging / safety limits), and optional SQLite archiving.
"""
import copy

import pandas as pd
import streamlit as st

from optplat import (
    Evaluator,
    HardwareEvaluator,
    Orchestrator,
    SafetyLimits,
    SimulatedMeter,
    SimulatedStage,
    SQLiteStore,
)
from optplat.demo import DEMO_PIPELINE, TWO_PHASE_PIPELINE, demo_vocs, optical_bench

st.set_page_config(page_title="光器件优化平台 Demo", layout="wide")

ONE_VAR = {"quadratic_fit", "gaussian_fit", "formula", "parametric_fit"}
ALGOS = [
    "grid_scan", "coordinate_descent", "nelder_mead",
    "quadratic_fit", "gaussian_fit", "parametric_fit", "formula", "bayesian",
]

# ---------------- sidebar: controls ----------------
st.sidebar.title("控制台")

workflow = st.sidebar.selectbox("工作流", [
    "两阶段：找光 → 精调 → 均衡",
    "交替循环：拟合 + keep 约束 + 回退",
    "自定义单步（挑一个算法试）",
])

vocs = demo_vocs()
st.sidebar.subheader("变量范围")
for name, v in vocs.variables.items():
    lo, hi = st.sidebar.slider(name, -10.0, 10.0, (float(v.low), float(v.high)), 0.5)
    vocs.variables[name].low, vocs.variables[name].high = lo, hi

# resolve the pipeline
if workflow.startswith("两阶段"):
    pipeline = copy.deepcopy(TWO_PHASE_PIPELINE)
elif workflow.startswith("交替"):
    pipeline = copy.deepcopy(DEMO_PIPELINE)
else:
    st.sidebar.subheader("自定义单步")
    algo = st.sidebar.selectbox("算法", ALGOS)
    obj = st.sidebar.selectbox("目标", list(vocs.objectives))
    if algo in ONE_VAR:
        chosen = [st.sidebar.selectbox("变量（单变量算法）", list(vocs.variables), index=2)]
    else:
        chosen = st.sidebar.multiselect("变量", list(vocs.variables),
                                        default=["x1", "x2"]) or ["x1"]
    step = {"stage": algo, "algorithm": algo, "variables": chosen,
            "objective": obj, "stop": {"max_iter": 300}}
    if algo in ("grid_scan", "line_scan"):
        step["n_per_axis"] = st.sidebar.slider("每轴点数", 3, 15, 7)
    if algo in ("quadratic_fit", "gaussian_fit", "parametric_fit"):
        step["n_samples"] = st.sidebar.slider("采样点数", 3, 11, 5)
        step["r2_gate"] = st.sidebar.slider("R² 守门", 0.0, 0.99, 0.9)
    if algo == "parametric_fit":
        step["model"] = st.sidebar.selectbox("模型", ["gaussian", "quadratic"])
        if st.sidebar.checkbox("钉死顶点 center=0.6（非标拟合演示）"):
            step["fixed"] = {"center": 0.6}
    if algo == "bayesian":
        step["sampler"] = st.sidebar.selectbox("采样器", ["tpe", "gp", "random"])
        step["n_calls"] = st.sidebar.slider("评估预算 n_calls", 10, 100, 40)
    pipeline = {"flow": [step]}

st.sidebar.subheader("评估方式")
mode = st.sidebar.radio("evaluator", ["纯函数（快）", "模拟硬件（噪声/平均/安全）"])
use_hw = mode.startswith("模拟")
if use_hw:
    noise = st.sidebar.slider("测量噪声 σ", 0.0, 0.05, 0.01, 0.005)
    averages = st.sidebar.slider("每点平均次数", 1, 10, 5)
    safety_on = st.sidebar.checkbox("启用安全限位（clamp 到变量范围）", True)
archive = st.sidebar.checkbox("归档到 SQLite", True)

# ---------------- main: problem + pipeline ----------------
st.title("光器件优化平台 · Demo")
st.caption("VOCS · Generator(ask/tell) · Orchestrator(scan→optimize / if / loop / keep / fallback) — 全 permissive 依赖")

c1, c2 = st.columns([1, 1])
with c1:
    st.subheader("问题声明 (VOCS)")
    st.table(pd.DataFrame([
        {"变量": n, "范围": f"[{v.low:g}, {v.high:g}]"} for n, v in vocs.variables.items()
    ]))
    st.markdown("目标：`y1`=耦合功率(max)，`y2`=均衡指标(max)；保持 `y1>0.8`")
with c2:
    st.subheader("编排流水线 (IR)")
    st.json(pipeline, expanded=False)

with st.expander("算法库（8 种）"):
    st.table(pd.DataFrame([
        ["grid_scan / line_scan", "找光", "网格/线扫描到阈值"],
        ["coordinate_descent", "局部优化", "坐标下降"],
        ["nelder_mead", "局部优化", "单纯形下降"],
        ["quadratic_fit / gaussian_fit", "标准拟合", "最小二乘 + R² 守门"],
        ["parametric_fit", "非标拟合/公式法", "钉死已知参数 + 自定义模型"],
        ["formula", "解析特例", "三点抛物线峰"],
        ["bayesian", "贝叶斯 (Optuna)", "TPE / GP 全局优化"],
    ], columns=["algorithm", "类别", "说明"]))

# ---------------- run ----------------
if st.button("▶ 运行优化", type="primary", use_container_width=True):
    store = SQLiteStore(":memory:", run_id="demo") if archive else None
    if use_hw:
        stage = SimulatedStage(vocs.initial_point())
        meter = SimulatedMeter(stage, optical_bench, noise=noise, seed=0)
        safety = SafetyLimits({n: (v.low, v.high) for n, v in vocs.variables.items()}) \
            if safety_on else None
        evaluator = HardwareEvaluator(stage, meter, averages=averages,
                                      safety=safety, store=store)
    else:
        evaluator = Evaluator(optical_bench, store=store)

    result = Orchestrator(vocs, evaluator, pipeline).run()

    m1, m2, m3 = st.columns(3)
    m1.metric("y1 (耦合功率)", f"{result['objectives'].get('y1', float('nan')):.4g}")
    m2.metric("y2 (均衡)", f"{result['objectives'].get('y2', float('nan')):.4g}")
    m3.metric("评估次数", result["n_evals"])

    left, right = st.columns([2, 1])
    with left:
        st.subheader("收敛曲线")
        hist = pd.DataFrame(result["history"])
        ycols = [c for c in ("y1", "y2") if c in hist.columns]
        st.line_chart(hist[ycols])
    with right:
        st.subheader("最优操作点")
        st.json({k: round(v, 4) for k, v in result["state"].items()})
        if archive:
            st.caption(f"已归档 {len(store.history())} 条到 SQLite "
                       f"(run_id=demo) · 支持断点续跑 / 一键回滚")

    st.subheader("编排轨迹")
    st.text("\n".join(result["events"]))
    with st.expander("完整评估历史"):
        st.dataframe(hist, use_container_width=True)
