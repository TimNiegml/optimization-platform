---
name: optplat-coupling-optimizer
description: >
  驱动「光器件耦合/标定优化平台」。当用户想搭一条优化流程、加/换算法节点、加载以前的方案、
  跑仿真、对比不同策略、或让平台自动调优（找光→精调→拟合的多阶段优化，用于光器件耦合、
  标定、WDL 均衡等），并希望结果实时显示在平台画布上时，使用本 skill。平台通过 MCP 暴露一组
  工具（见 connect.json 里的 optplat 服务器）；本 skill 教你如何用好它们、并把改动推到画布自动刷新。
---

# 光器件耦合优化平台 · Agent 操作指南

你（Agent）通过 MCP 服务器 `optplat` 驱动这个平台。连接配置见同目录 `connect.json`
（HTTP + Bearer token）。平台=服务器，你=客户端。**所有运行都经过统一执行引擎与硬件安全限位，
你无法绕过、也不应尝试绕过**——你只提交声明式流程（graph IR），平台负责安全执行。

## 0. 核心概念（先读）

- **变量/目标**：默认自变量 `x1,x2,x3`，因变量 `y1`（耦合功率，最大化）、`y2`（WDL/偏振均衡）。
  用 `get_default_vocs` 确认范围与通道成本。
- **流程 = graph IR**：`{nodes:[{id,type,data:{algorithm,variables,objective,...}}], edges, until?}`。
  你几乎不用手写 JSON——用 `new_workflow` + `add_algorithm_node` 逐步搭。
- **三阶段范式**：`找光/粗调 → 精调 → 拟合/定值`。绝大多数耦合任务照此搭：
  1. 找光：`grid_scan`/`line_scan`（扫到阈值即停，用 `stop_target`），或多峰用 `bayesian` 全局。
  2. 精调：`nelder_mead`（多变量峰）/`coordinate_descent`（稳健）/`gradient_ascent`（沿脊快速对准）。
  3. 拟合/定值：`gaussian_fit`/`quadratic_fit`/`formula`/`parametric_fit`（对 `x3→y2` 定峰/均衡），
     常配 `keep:"y1>0.8"` 保持耦合不塌。
- **会话 session**：用户在平台画布『实时会话』里填了一个**会话 ID**。你**每次改动流程/跑完/调完，
  都要带上同一个 session 调用 `push_to_canvas`/`run_workflow(session=...)`/`autotune(session=...)`，
  画布就会自动刷新显示——这是「用自然语言改，界面自动更新」的关键。开场先问用户的会话 ID（默认 `default`）。

## 1. 标准工作流（你的主循环）

1. **明确意图**：用户要新搭、还是改现有、还是加载旧方案？在哪个仿真场景（`list_benches`）？目标判据是什么？
2. **拿能力清单**（需要时）：`list_algorithms`（算法+参数）、`list_benches`、`list_solutions`（历史方案）。
3. **构建流程**：
   - 从零：`new_workflow(until=...)` → 多次 `add_algorithm_node(...)`。
   - 复用：`load_solution(name)` 取回 graph，再按需 `add_algorithm_node` 增改。
4. **推到画布**：`push_to_canvas(graph, session, bench, note="一句话说明你改了什么")`。用户立刻在界面看到。
5. **运行/对比/调优**（都带 `session`，结果自动上画布）：
   - `run_workflow(graph, bench, session=...)` → 看 objectives/耗时/拟合。
   - `compare_strategies([{name,graph|solution}...], bench, objective)` → 排名 + 推荐。
   - `autotune(bench, target, quality_weight, time_weight, stability_weight, session=...)` → 帕累托候选，
     把用户选中的候选 `push_to_canvas` 上画布。
6. **汇报**：用 `explain_result` 或自己把 JSON 讲成人话；需要时读回用户手改用 `get_canvas(session)`。

## 2. 自然语言 → 流程（NL→IR 起草配方）

把用户的话拆成三阶段，逐节点 `add_algorithm_node`。示例：

> 「多峰场景，先全局找主瓣，再精调，最后把 x3 拟合到 y2 均衡，全程保持 y1>0.8，两个都达标就停」

```
g = new_workflow(until="y1>=0.95 and y2>=0.9")
g = add_algorithm_node(g, "bayesian",     ["x1","x2"], "y1", params={"n_calls":60})
g = add_algorithm_node(g, "nelder_mead",  ["x1","x2"], "y1")
g = add_algorithm_node(g, "gaussian_fit", ["x3"],      "y2", keep="y1>0.8")
push_to_canvas(g, session=<用户会话>, bench="multi_peak", note="贝叶斯全局+单纯形+高斯拟合")
run_workflow(g, bench="multi_peak", session=<用户会话>)
```

选型速记：多峰/强局部极值→`bayesian` 找光；相关谷（如 Rosenbrock）→`nelder_mead`；
偏斜/尖峰→`gradient_ascent`；单峰→`grid_scan`+`nelder_mead`。钟形 y2 用 `gaussian_fit`，
已知参数要钉死用 `parametric_fit`。更多见 `reference/recipes.md`；IR/参数细节见 `reference/ir_schema.md`。

## 3. 呈现结果（用户/平台都能解析）

- 运行结果：报 `objectives`（最终 y1/y2）、`state`（最优 x）、`sim_seconds`（测量耗时）、`fits`（拟合公式）。
- 对比：报 `ranking` 与 `recommended`，并说明为什么（质量/耗时/评估次数差异）。
- 自动调优：报 top 候选的 质量/时长/稳定/utility，标出帕累托最优；建议用户采用哪条并解释权衡。
  想让用户在画布上直接看/跑某候选，就 `push_to_canvas(候选.graph, session=...)`。

## 4. 纪律

- 改动前后都用同一个 `session`，否则画布不会刷新。
- 不臆造算法名/变量名；不确定就先 `list_algorithms`/`get_default_vocs`。
- 遇到平台校验报错（未知算法、非法表达式），读错误信息修正后重试，不要绕过安全设置。
- 破坏性操作（`delete_solution`、覆盖同名 `save_solution`）先跟用户确认。
