"""Streamlit UI for the optimization platform.

Run:  streamlit run app.py

The thin form-based surface a non-coder uses: pick a workflow, adjust variable
ranges, hit Run, watch the convergence curve and the orchestration trace. The
drag-drop canvas and the GLM5.1 copilot are later phases; they edit the same
VOCS + pipeline objects this page does.
"""
import copy

import pandas as pd
import streamlit as st

from optplat import Evaluator, Orchestrator
from optplat.demo import DEMO_PIPELINE, TWO_PHASE_PIPELINE, demo_vocs, optical_bench

st.set_page_config(page_title="Optimization Platform", layout="wide")
st.title("光器件优化平台")
st.caption("VOCS · Generator(ask/tell) · Orchestrator(scan→optimize / if / loop / keep / fallback) — 全 BSD/MIT/Apache 依赖")

PIPELINES = {
    "两阶段：找光(grid) → 精调(Nelder-Mead) → 均衡(公式法)": TWO_PHASE_PIPELINE,
    "交替循环：拟合优化 + keep 约束 + 拟合失败回退": DEMO_PIPELINE,
}

vocs = demo_vocs()
left, right = st.columns([1, 2])

with left:
    st.subheader("① 选择工作流")
    choice = st.radio("pipeline", list(PIPELINES), label_visibility="collapsed")

    st.subheader("② 变量范围 (VOCS)")
    for name, v in vocs.variables.items():
        lo, hi = st.slider(f"{name}", -10.0, 10.0, (float(v.low), float(v.high)), 0.5)
        vocs.variables[name].low, vocs.variables[name].high = lo, hi

    st.subheader("③ 目标 & 约束")
    st.markdown(
        "- `y1` = 耦合功率 → **maximize**\n"
        "- `y2` = 均衡指标 → **maximize**，保持 `y1 > 0.8`\n"
        "- 全局早停：`y1>=0.98 and y2>=0.95`"
    )
    run = st.button("▶ 运行优化", type="primary", use_container_width=True)

with right:
    st.subheader("④ 算法库")
    st.table(pd.DataFrame([
        ["grid_scan / line_scan", "找光 (Phase 1)", "网格/线扫描到阈值"],
        ["coordinate_descent", "局部优化", "坐标下降 (compass search)"],
        ["nelder_mead", "局部优化", "单纯形下降"],
        ["quadratic_fit / gaussian_fit", "标准拟合", "最小二乘 + R² 守门"],
        ["parametric_fit", "非标拟合/公式法", "钉死已知参数 + 自定义模型"],
        ["formula", "解析特例", "三点抛物线峰 (无回归)"],
    ], columns=["algorithm", "类别", "说明"]))

    st.subheader("⑤ 编排流水线")
    st.json(PIPELINES[choice], expanded=False)

if run:
    evaluator = Evaluator(optical_bench)
    orch = Orchestrator(vocs, evaluator, copy.deepcopy(PIPELINES[choice]))
    result = orch.run()

    st.success(
        f"完成 · 评估 {result['n_evals']} 次 · "
        f"y1={result['objectives']['y1']:.4g} · y2={result['objectives']['y2']:.4g}"
    )
    c1, c2 = st.columns([2, 1])
    with c1:
        st.subheader("收敛曲线")
        hist = pd.DataFrame(result["history"])
        st.line_chart(hist[["y1", "y2"]])
    with c2:
        st.subheader("最优操作点")
        st.json({k: round(v, 4) for k, v in result["state"].items()})

    st.subheader("编排轨迹")
    st.text("\n".join(result["events"]))
    with st.expander("完整评估历史"):
        st.dataframe(hist, use_container_width=True)
