"""Streamlit UI for the optimization platform MVP.

Run:  streamlit run app.py

This is the thin form-based surface a non-coder uses: adjust variable ranges,
review the pipeline, hit Run, watch the convergence curve and the orchestration
trace. The drag-drop canvas and the GLM5.1 copilot are later phases; they would
edit the same VOCS + pipeline objects this page does.
"""
import copy

import pandas as pd
import streamlit as st

from optplat import Evaluator, Orchestrator
from optplat.demo import DEMO_PIPELINE, demo_vocs, optical_bench

st.set_page_config(page_title="Optimization Platform (MVP)", layout="wide")
st.title("光器件优化平台 · MVP")
st.caption("VOCS · Generator(ask/tell) · Orchestrator(if / loop-until / keep / fallback) — 全 BSD/MIT/Apache 依赖")

vocs = demo_vocs()

left, right = st.columns([1, 2])

with left:
    st.subheader("① 变量范围 (VOCS)")
    ranges = {}
    for name, v in vocs.variables.items():
        lo, hi = st.slider(f"{name}", -10.0, 10.0, (float(v.low), float(v.high)), 0.5)
        ranges[name] = (lo, hi)
        vocs.variables[name].low, vocs.variables[name].high = lo, hi

    st.subheader("② 目标 & 约束")
    st.markdown(
        "- `y1` = 耦合功率 → **maximize**\n"
        "- `y2` = 均衡指标 → **maximize**，且保持 `y1 > 0.8`\n"
        "- 全局早停：`y1>=0.98 and y2>=0.9`"
    )
    run = st.button("▶ 运行优化", type="primary", use_container_width=True)

with right:
    st.subheader("③ 编排流水线 (pipeline)")
    st.code(
        "align_y1        坐标梯度  x1,x2 → max y1\n"
        "loop (≤6轮, until y1≥.95 & y2≥.9):\n"
        "  balance_y2    二次拟合  x3    → max y2   [keep y1>0.8, R²门→回退坐标梯度]\n"
        "  refine_y1     坐标梯度  x1,x2 → max y1",
        language="text",
    )

if run:
    evaluator = Evaluator(optical_bench)
    orch = Orchestrator(vocs, evaluator, copy.deepcopy(DEMO_PIPELINE))
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
