# 灵敏度采集 · 完整用法

**测量，不是优化。** 给一个原点，把每个自变量单独动一动（其余轴钉在原点），
看每个因变量怎么变，得到 ∂y/∂x 与线性范围。接完硬件后应该做的第一件事。

---

## 1. 什么时候该跑

| 场景 | 为什么先测 |
|---|---|
| 刚接完一台新设备 | 验证每个轴真的能推动每个 y；某格 ∂y/∂x≈0 通常是接线/使能/量纲问题，不是算法问题 |
| 要用 `damped_sensitivity` 多进多出定值 | 它的 `sensitivity` 入参就是这张矩阵。手填的经验值往往过时或量纲不对 |
| 要设扫描/精调的步距 | 步距必须落在线性范围内，否则拟合定峰和梯度方向都会失真 |
| 换了工作点/温度/器件批次 | 多原点采集能直接量出灵敏度漂移多少 |
| 优化"莫名其妙不收敛" | 先看是不是根本没响应、或工作点已经在非线性区 |

---

## 2. 接口

```python
from optplat.sensitivity import SensitivitySpec, run_sensitivity

spec = SensitivitySpec(
    variables=["x1", "x2"],        # 要动的 x（个数任意，按场景来）
    objectives=["y1", "y2"],       # 要测的 y；留空 = 全部
    step=0.2,                      # 统一步距（绝对值）
    steps={"x2": 0.05},            # 每轴步距，覆盖 step（量纲差别大时必用）
    n_points=7,                    # 每轴点数（含原点，建议奇数）
    origins=[{"x1": 2, "x2": 0}],  # 原点列表；留空 = 当前操作点
    fit="linear",                  # linear | quadratic
    linear_tol=0.05,               # 线性范围判据：偏离 ≤ 5% × y跨度
    reuse_origin=True,             # 原点只测一次，各轴曲线共用
    settle_at_origin=True)         # 采完回到原点

out = run_sensitivity(vocs, evaluator, spec, start_point=None, eval_budget=2000)
```

四个等价入口，同一套核心：

| 入口 | 用法 | 适合 |
|---|---|---|
| 画布『📐 灵敏度采集』 | 顶栏按钮 | 人工看曲线、判断线性范围 |
| `POST /sensitivity` | 见 SKILL.md §8 | 脚本/集成 |
| `sensitivity_scan` 节点 | 放进流程图（单原点） | "先测再解"的自动流程 |
| MCP `measure_sensitivity` | Agent 调用 | 让 Agent 自己去测 |

---

## 3. 测量次数与耗时

```
每个原点 = 1（原点，reuse_origin 时只测一次）
         + Σ_轴 (n_points − 1)
总计    = n_origins × 上式 + 1（结束回原点）
```

例：2 轴 × 7 点 × 1 原点 = 1 + 2×6 + 1 = **14 次**。
真实台架上一次测量按秒算，所以别一上来就 15 点 × 5 原点。

只读被选中的通道（`objectives`），没选的 y **一次都不会读**，也不计其耗时。

---

## 4. 结果字段逐条

```python
out = {
  "variables": [...], "objectives": [...], "fit": "linear",
  "matrix":     {y: {x: ∂y/∂x}},     # ← 多原点时是各原点均值
  "matrix_std": {y: {x: 标准差}},     # ← 单原点全 0；相对值大 = 灵敏度随工作点漂移
  "origins": [ {                      # 每个原点一份
      "origin": {x: 值},
      "curves": {x: {"xs":[...], "y": {y: [...]}}},   # 原始采集点，作图/CSV 用
      "fits":   {y: {x: {...见下...}}},
      "matrix": {y: {x: 斜率}},
  } ],
  "n_evals": int, "sim_seconds": float, "reads": {y: 次数},
  "warnings": [...], "summary": [...],   # summary = 给人看的一句话结论
}
```

每对 x→y 的 `fits[y][x]`：

| 字段 | 含义 | 怎么用 |
|---|---|---|
| `slope` | **原点处**的 ∂y/∂x | 进矩阵。二次拟合时 = `2c₂x₀+c₁`，不是整段直线斜率 |
| `r2` | 直线拟合优度 | <0.98 就要看看是不是扫出线性区了 |
| `rmse` | 残差均方根 | 与噪声水平比：接近噪声 = 拟合已经到极限 |
| `curvature` | d²y/dx²（仅二次） | 非零=有弯曲；浮点噪声级会被归零 |
| `linear_range` | **实测**线性区间（绝对坐标） | 步距/扫描窗口应落在里面。`None`=该通道无响应 |
| `linear_span` | 线性区宽度 | 直接拿来定 `span_frac` 或 `step` |
| `linear_full` | 整段扫描都线性 | True 说明还没扫到非线性，可以扫更宽 |
| `max_dev_frac` | 最大偏离 / y跨度 | 线性度的单一数字 |
| `y_span` / `y0` | y 跨度 / 原点处拟合值 | 判断信噪比 |
| `note` | 异常说明 | y 跨度≈0 时出现 |

---

## 5. 与阻尼灵敏度求解的衔接

`matrix` 的形状 `{y: {x: 值}}` 就是 `damped_sensitivity` 节点 `sensitivity` 入参的形状。

```python
graph = {"nodes": [
  {"id": "solve", "type": "algorithm", "data": {
      "algorithm": "damped_sensitivity",
      "variables": out["variables"],
      "objective": out["objectives"][0],
      "targets": {"y1": 2.0, "y2": 0.0},
      "sensitivity": out["matrix"],        # ← 实测矩阵直接喂进去
      "damping": 0.8}}, ...]}
```

画布上有一键『↧ 填入阻尼灵敏度节点』做同样的事。

**衔接前先看两件事**：
1. 各 `linear_range` 是否覆盖你期望的调节行程——阻尼最小二乘假设局部线性；
2. 多原点的 `matrix_std` 是否够小——漂移大就该在目标工作点附近重测，而不是用全局平均。

---

## 6. 常见误读

| 现象 | 别急着下的结论 | 实际多半是 |
|---|---|---|
| 线性范围比扫描范围窄 | "采集不准" | 正常且有用——那才是能当线性用的区间 |
| 某格 ∂y/∂x 是 0 | "算法有问题" | 该轴没接/没使能/该 y 本来就不依赖它 |
| 换原点后矩阵变了 | "重复性差" | 真实的非线性。看 `matrix_std` 的相对值，>20% 该分段建模 |
| 加噪声后斜率跳 | "拟合不稳" | 提高 `averages` 而不是加点数：平均降噪 √N |
| R² 很高但线性范围很窄 | 矛盾 | R² 是全段直线拟合，线性范围是相对**原点切线**的偏离——曲线两端对称弯曲时正是这个组合 |
| warnings 里有"超出量程" | 可忽略 | 该点被夹到边界，会与相邻点重合并**拉偏斜率**。减小步距或点数后重测 |
