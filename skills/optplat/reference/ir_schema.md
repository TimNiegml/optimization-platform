# 流程 IR 与工具参考

## graph IR 结构

```jsonc
{
  "nodes": [
    {"id": "start", "type": "start"},
    {"id": "n1", "type": "algorithm", "data": {
        "algorithm": "grid_scan",            // 算法英文 ID（list_algorithms 的 name）
        "variables": ["x1", "x2"],            // 这一步优化哪些自变量
        "objective": "y1",                    // 这一步优化哪个因变量
        "stage": "网格扫描",                   // 可选，显示名
        "stop": {"target": "y1>0.2"},         // 可选，本步提前停止条件
        "keep": "y1>0.8",                     // 可选，软约束（优化本步时保持成立）
        "n_per_axis": 7                        // 可选，算法参数（见各算法 params）
    }},
    {"id": "end", "type": "end"}
  ],
  "edges": [
    {"source": "start", "target": "n1"},
    {"source": "n1", "target": "end"},
    {"source": "refine", "target": "balance", "condition": "not (y1>=0.95 and y2>=0.9)"}  // 条件边=分支/循环
  ],
  "until": "y1>=0.98 and y2>=0.95"            // 可选，全局早停：满足即整体停止
}
```

- 用 `new_workflow` + `add_algorithm_node` 生成，通常无需手写。
- 分支/循环：一条边带 `condition` 就是条件边；指向更早的节点即回边（循环）。循环有 `max_visits` 与全局
  `max_steps` 限幅，不会失控。
- 表达式（`until`/`stop.target`/`keep`/`condition`）白名单：比较运算、`and/or/not`、`abs/min/max`、变量名。

## 工具清单

| 工具 | 关键参数 | 返回 |
|---|---|---|
| `list_algorithms` | — | 算法 name/label/category/single_var/params/desc |
| `list_benches` | — | 仿真场景 name/label/desc |
| `get_default_vocs` | — | 变量范围 + 目标方向 + 通道成本 |
| `new_workflow` | until? | 空 graph（start/end 骨架） |
| `add_algorithm_node` | graph, algorithm, variables, objective, params?, keep?, stop_target? | 更新后的 graph |
| `list_solutions` | — | 内置示例 + 已保存方案（name/source/bench/steps/until） |
| `load_solution` | name | 方案记录（含可运行 graph） |
| `save_solution` | name, graph, bench, description | 保存结果 |
| `delete_solution` | name | 删除结果（内置不可删） |
| `run_workflow` | graph, bench?, noise?, averages?, safety?, session? | objectives/state/n_evals/sim_seconds/fits/events |
| `compare_strategies` | strategies[{name,graph\|solution}], bench?, objective? | results/ranking/recommended |
| `autotune` | bench?, target?, *_weight, noise_levels?, n_trials?, max_candidates?, top_k?, session? | ranked 候选（含 graph/指标/utility/pareto） |
| `push_to_canvas` | graph, session, bench?, note? | revision（画布据此自动刷新） |
| `get_canvas` | session | 会话当前 graph/bench/result/autotune（含用户手改） |
| `explain_result` | result | 中文小结 |

## 算法一览（category → 阶段）

- `grid_scan` / `line_scan` （find-light，找光/粗调）
- `bayesian` （bayesian，全局找光，多峰/昂贵/多变量优先）
- `coordinate_descent` / `nelder_mead` / `gradient_ascent` （local，精调）
- `quadratic_fit` / `gaussian_fit` （fit，单变量定峰）
- `parametric_fit` （fit，非标拟合：钉死已知参数、支持自定义模型表达式）
- `formula` （analytic，三点解析定峰）

单变量算法（fit/analytic）只能驱动单变量阶段（如 `x3→y2`）。
