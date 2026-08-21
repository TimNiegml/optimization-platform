# 代码构造到画布映射

| 代码构造 | graph IR | 说明 |
|---|---|---|
| 顺序函数调用 | 节点 + 普通边 | 一次有意义的测量/优化动作对应一个节点 |
| 已登记优化器 | `algorithm` | 名称必须来自 `list_algorithms` |
| 只读标量/矩阵采集 | `observer` | `kind`: `scalar`、`matrix` 或 `matrix_plot` |
| `for ... in range(N)` | `for_loop` | `iterations=N`；`body`/`exit` 两条角色边；体末回边 |
| `if/elif/else` | 条件边 | 条件使用平台安全表达式；保留一条默认边 |
| `while condition` | 条件回边 | 设置 `max_visits`/全局 `max_steps`，不可无界 |
| `break` | 指向循环后继的条件边 | 明确退出判据 |
| `z=f(y...)` | derived objective | 使用受限表达式；矩阵变换若 DSL 不支持则记为缺口 |
| 矩阵→执行器向量 | `matrix_policy` | 显式 `output_map` 分配到 `x1,x2,...` |
| 未登记模型/硬件/变换 | 缺口卡 | 不生成冒充可运行的 algorithm 节点 |

固定循环示例：

```json
{
  "nodes": [
    {"id":"loop","type":"for_loop","data":{"iterations":3}},
    {"id":"measure","type":"observer","data":{"kind":"scalar","channels":["y1"]}},
    {"id":"end","type":"end"}
  ],
  "edges": [
    {"source":"loop","target":"measure","role":"body"},
    {"source":"measure","target":"loop"},
    {"source":"loop","target":"end","role":"exit"}
  ]
}
```
