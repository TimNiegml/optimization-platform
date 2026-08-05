# 设备接口 · 完整规范

本文件是 `optplat/userdev.py` 的契约说明。实现方（用户）与调用方（平台）各自的义务写清楚，
任何一方偏离都会在真实台架上表现为难查的偶发问题。

---

## 1. 模块级契约

设备文件必须导出二者之一：

```python
AXES: list[Axis]        # 自变量 x，顺序即画布/VOCS 里的顺序
METERS: list[Meter]     # 因变量 y，顺序即目标顺序
```

或

```python
def build() -> tuple[list[Axis], list[Meter]]: ...
```

`build()` 优先于 `AXES/METERS`（`_axes_meters()` 先查 `build`）。需要连接仪器、读配置、
上电初始化的场景都用 `build()`——模块导入时的副作用越少越好，因为 `load_device()` 会
**执行整个文件**。

至少一个 axis、一个 meter，否则 `DeviceSpec` 直接抛 `ValueError`。

---

## 2. Axis 契约

平台只通过属性访问，不做 isinstance 检查——自己写类、用 dataclass、包一层现成驱动都行。

### 你必须提供

| 成员 | 类型 | 语义 |
|---|---|---|
| `name` | `str` | 变量名。进 VOCS `variables`，图 IR 的 `variables: ["x1", ...]` 引用它。**必须唯一**，且不要和目标名重名 |
| `low`, `high` | `float` | 软件量程。算法只在此区间内搜索；画布量程条按它画 |
| `move(value: float) -> None` | | 命令轴到绝对位置。**应阻塞到运动结束**；若不阻塞，必须用 `settle_time` 覆盖运动时间 |
| `get() -> float` | | 读**真实当前位置**（编码器/控制器回读）。不要返回"上次下发的目标值"——两者在丢步、限位、未到位时会不一致，而平台用它当优化起点 |

### 可选

| 成员 | 语义 |
|---|---|
| `resolution` | 最小可分辨步距，进 `Variable.resolution` 供算法参考 |

### 平台的义务

- 在**每次** `evaluate()` 里，对 `x` 先过安全限位再逐轴 `move()`；未出现在 `x` 里的轴不动。
- 起点：`DeviceSpec.current_point()` = 所有轴的 `get()`。REST/画布不显式传 `start_point` 时用它。
- 单位由你定义，平台不做换算——`low/high/resolution/移动指令` 必须同一单位。

---

## 3. Meter 契约

### 你必须提供

| 成员 | 语义 |
|---|---|
| `name` | 目标名 y，进 VOCS `objectives`；条件表达式（`keep`/`stop`/`until`）里就写它 |
| `get() -> float` | 读一次测量。**一次调用 = 一次采集**（除非来自 `Source`，见 §4） |

### 可选（都进 VOCS `Objective`）

| 成员 | 默认 | 语义 |
|---|---|---|
| `mode` | `"maximize"` | `maximize` / `minimize` / `target` |
| `target` | `None` | `mode="target"` 时的目标值 |
| `cost` | `0.0` | 读一次的秒数，用于耗时模型与画布预估（不影响真实等待） |
| `group` | `None` | 计时分组：同组并行（取 max），不同组串行（相加），未分组=自成一组 |
| `device` / `param` | `None` | 通道映射标注（"光功率计" / "power"），画布显示用 |
| `source` / `key` | `None` | 由 `Source.meter()` 填，见 §4；手写也可以 |

### 平台的义务

- **选择性读取**：`evaluate(x, stage, channels)` 只调用 `channels` 里的 meter。`channels` 由
  `StageEngine._needed_channels` 算出＝该步骤的 目标 ∪ `keep` ∪ `stop` ∪ `until` ∪ `targets` 所引用的 y。
  → 只优化 y1 的步骤**根本不会调用 y2.get()**，真实测量时间省下来。
- **平均**：`averages=N` → 每个通道读 N 次取算术平均。N 由画布/API 传，不是你的事。
- **计时**：`sim_seconds += averages × read_seconds(keys, costs, groups)`。

---

## 4. Source 契约（一次采集 → 多通道）

### 存在的理由

一台仪器一次触发返回一组读数（双通道功率计 `read()` → power1+power2；OSA 一次扫描出
峰值功率+中心波长+带宽）。若给每个 y 各写一个 `read_fn`：

1. 仪器被触发 N 次 → 慢 N 倍；
2. 各 y 来自**不同次采集** → 噪声不同、时刻不同，"优化 y1 同时 keep y3>k" 的判断建立在
   不一致的物理量上。

### 接口

```python
Source(name, read_fn, cost=0.0, group=None, device=None)
    read_fn() -> dict[str, float]     # 一次触发的全部读数，键=通道名
    .read()        -> dict            # 触发一次并缓存；缓存在则直接返回（平台调用）
    .invalidate()  -> None            # 丢弃缓存（平台调用）
    .meter(y_name, key=None, mode=..., target=..., cost=..., group=..., device=..., param=...) -> Meter
```

`meter()` 派生的通道：`cost/group/device` 缺省继承 Source；`group` 默认 `"src:<name>"`
→ 同一次采集的通道按**并行**计时，采集时间算一次。

### 缓存边界（关键，别自己实现）

平台在 `DeviceEvaluator.evaluate()` 里：

```
for _ in range(averages):
    for src in 本步骤用到的 sources:  src.invalidate()
    for k in 本步骤需要的通道:        acc[k] += meters[k].get()
```

即 **缓存的生命周期 = 一轮平均**。由此保证：

| 场景 | 结果 |
|---|---|
| 一步要 y1+y3（同 Source） | 仪器触发 **1 次**，两值同源 |
| `averages=4`，要 y1+y3 | 触发 **4 次**（不是 8，也不是 1）→ 4 个独立噪声样本 |
| 某步只要 y2（另一台仪器） | 该 Source **完全不触发** |
| 计时 | y1+y3 = 一次采集的 `cost`；y1+y2（不同仪器）= 两者相加 |

**这就是"用户不能自己缓存"的原因**：设备文件里看不到采集边界，自己缓存会让 `averages=8`
变成同一个读数重复 8 次，平均失效且噪声评估全错。

### read_fn 的义务

- 每次调用**真的触发一次采集**，不要自己缓存。
- 返回的 dict **必须包含所有已声明的 key**。缺 key → `Meter.get()` 抛 `KeyError`；
  通道数会变的仪器（比如 OSA 峰数不定）请在 `read_fn` 内补默认值/哨兵值。
- 返回值可转 `float`。

---

## 5. DeviceSpec / DeviceEvaluator（平台侧，了解即可）

```python
spec = load_device("your_device.py")     # 执行文件 → 解析 AXES/METERS
spec.vocs()            -> VOCS           # 自动读出 n_x / n_y，含 mode/target/cost/group/device/param
spec.current_point()   -> dict           # 各轴 get()，优化起点
spec.costs() / .groups()                 # 供耗时模型
spec.evaluator(averages=1, safety=None, settle_time=0.0) -> DeviceEvaluator
spec.info()            -> dict           # /device 端点返回的设备概况
```

`DeviceEvaluator.evaluate(x, stage="", channels=None)` 的完整时序：

1. `safety.enforce(x)` —— 独立硬限位，默认 **clamp 到边界**，`strict=True` 则抛 `SafetyViolation`；
2. 对 `target` 里每个已知轴 `move(值)`；
3. `settle_time` 睡眠（若 >0）；
4. 确定 `keys`（`channels` 为 None 则全部通道）；
5. 取本步骤涉及的 sources（按对象身份去重）；
6. 循环 `averages` 轮：先 `invalidate()` 这些 source，再逐通道 `get()` 累加；
7. 求平均、累计 `reads`/`sim_seconds`、追加 `history`、返回 `{y: 值}`。

返回给引擎的就是普通 dict，**算法层完全不知道下面是仿真还是真实仪器**。

---

## 6. 与 `HardwareEvaluator`（老路径）的差异

`optplat/hardware.py` 的 `Stage`/`Meter` 协议：

```python
stage.move(axis: str, value: float) -> None
meter.read() -> dict[str, float]        # 一次返回所有通道
```

| | `DeviceEvaluator`（设备文件） | `HardwareEvaluator`（Stage+Meter） |
|---|---|---|
| 选择性读取 | ✅ 只读本步需要的通道 | ❌ 每步 `read()` 全读 |
| 一次采集多通道 | ✅ `Source` | ✅ 天然（read 返回 dict） |
| 每轴独立驱动 | ✅ 每轴自己的 move/get | ⚠️ 单一 stage 按 axis 名分发 |
| 起点=当前位置 | ✅ `axis.get()` | ❌ 需外部提供 |
| 安全限位/平均/计时 | ✅ | ✅ |

新接硬件一律走设备文件。`HardwareEvaluator` 保留给仿真台（`SimulatedStage/SimulatedMeter`）
和已有集成。

---

## 7. 安全限位的两层，别混

| 层 | 在哪 | 作用 |
|---|---|---|
| VOCS 量程 `low/high` | `Axis.low/high` | **算法搜索边界**。算法不会主动越界，但拟合外推、坐标步进等仍需被夹 |
| 安全限位 `SafetyLimits` | `DeviceEvaluator(safety=...)` | **物理硬限位**，每次 move 前强制执行，可 clamp 可抛错。任何算法/编排 bug 都越不过它 |

两层独立。安全限位通常**比量程更紧或相等**，画布上"逐变量安全限位"填的就是它。
设备文件里**不要**再自己夹一层——会掩盖上层越界 bug，让问题查不出来。
