# 按场景的起草配方（NL → 三阶段流程）

每条给出「什么时候用 + 节点序列」。`session` 处填用户会话 ID。

## 单峰光纤耦合（single_peak）
一个高斯主瓣，最常见的找光→精调→均衡。
```
g = new_workflow(until="y1>=0.98 and y2>=0.95")
g = add_algorithm_node(g, "grid_scan",   ["x1","x2"], "y1", stop_target="y1>0.2")
g = add_algorithm_node(g, "nelder_mead", ["x1","x2"], "y1")
g = add_algorithm_node(g, "formula",     ["x3"],      "y2", keep="y1>0.8")
```

## 多峰光耦合（multi_peak）
主瓣+旁瓣，怕卡旁瓣 → 贝叶斯全局找主瓣。
```
g = new_workflow(until="y1>=0.95 and y2>=0.9")
g = add_algorithm_node(g, "bayesian",     ["x1","x2"], "y1", params={"n_calls":60})
g = add_algorithm_node(g, "nelder_mead",  ["x1","x2"], "y1")
g = add_algorithm_node(g, "gaussian_fit", ["x3"],      "y2", keep="y1>0.8")
```

## 偏斜/相关峰（skew_peak）
峰沿斜轴拉长、两轴相关 → 粗找后用梯度上升沿脊快速对准。
```
g = new_workflow(until="y1>=0.97 and y2>=0.92")
g = add_algorithm_node(g, "grid_scan",       ["x1","x2"], "y1", stop_target="y1>0.2")
g = add_algorithm_node(g, "gradient_ascent", ["x1","x2"], "y1")
g = add_algorithm_node(g, "formula",         ["x3"],      "y2", keep="y1>0.8")
```

## 相关谷 / Rosenbrock（rosenbrock）
狭长弯谷、强相关 → 密网格入谷 + 单纯形沿谷。
```
g = new_workflow(until="y1>=0.9 and y2>=0.8")
g = add_algorithm_node(g, "grid_scan",   ["x1","x2"], "y1", params={"n_per_axis":11}, stop_target="y1>0.1")
g = add_algorithm_node(g, "nelder_mead", ["x1","x2"], "y1")
g = add_algorithm_node(g, "formula",     ["x3"],      "y2", keep="y1>0.6")
```

## 强多峰 / Rastrigin（rastrigin）、外围平坦 / Ackley（ackley）
贝叶斯全局落盆地，再本地精调。
```
g = new_workflow(until="y1>=0.9 and y2>=0.8")
g = add_algorithm_node(g, "bayesian",           ["x1","x2"], "y1", params={"n_calls":80})
g = add_algorithm_node(g, "coordinate_descent", ["x1","x2"], "y1")   # ackley 换 gradient_ascent
g = add_algorithm_node(g, "formula",            ["x3"],      "y2", keep="y1>0.6")
```

## 常见追加需求
- **「保持 y1 不塌」** → 给拟合/精调节点加 `keep="y1>k"`。
- **「扫到有光就停」** → 找光节点加 `stop_target="y1>阈值"`。
- **「有噪声/要稳」** → `run_workflow(..., noise=0.02, averages=3)`；或 `autotune(stability_weight 调高)`。
- **「不知道哪种最好」** → `autotune(bench, target, session=...)`，把推荐候选 `push_to_canvas`。
- **「用之前那套」** → `list_solutions` 找名字 → `load_solution(name)` → 按需改。

复用内置方案名：`single_peak_default` / `multi_peak_bayes` / `skew_gradient` /
`rosenbrock_simplex` / `rastrigin_bayes` / `ackley_bayes`。
