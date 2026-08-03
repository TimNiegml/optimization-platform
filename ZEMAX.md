# 接入 Zemax OpticStudio（经 OpticStudioMCPServer）

> 目标：自变量 = 某个 **Coordinate Break** 的 Decenter X/Y、Thickness、Tilt About X/Y/Z（**跟随的
> Coordinate Break 自动同步**）；因变量 y = **Merit Function Editor** 的值。
> 同时**不影响真实设备接入**——Zemax 只是众多 backend 之一，可与仪器混跑。

```bash
python run_zemax.py                                   # 离线：内置假 OpticStudio 服务器，直接能跑
python run_zemax.py "C:/…/OpticStudioMCPServer.exe" "C:/designs/lens.zmx"   # 真机
```

---

## 1. 为什么能"插上就用"

平台里唯一接触真实世界的接口是 **Evaluator**：

```python
evaluate(x: dict[str, float], stage: str) -> dict[str, float]
```

算法层（ask/tell）、编排层（flow/if/loop、图运行时）、安全护栏都只认这个契约。所以 Zemax 不是新架构，
而是**第三个 backend**：

| backend | 文件 | y 从哪来 |
|---|---|---|
| `function` | `optplat/evaluator.py` | 一个 Python 函数（仿真台） |
| `hardware_sim` / 真实仪器 | `optplat/hardware.py` | 电机台 `move()` + 功率计 `read()` |
| **`zemax`** | **`optplat/zemax.py`** | **OpticStudio 的 Merit Function Editor** |
| `composite` | `optplat/zemax.py` | 上面几个**同时**跑，y 合并（模型在环 + 硬件在环） |

backend 和算法一样是**可注册插件**（`optplat/backends.py`），IR JSON 里选一行就切换：

```jsonc
"evaluator": {"mode": "zemax", "connection": {...}, "binding": {...}}
```

---

## 2. 链路

```
optplat 算法 (ask)                                    OpticStudio (Windows)
      │  x = {"dec_x": 0.12, "tilt_x": 0.30, ...}
      ▼
ZemaxEvaluator ── MCP stdio (JSON-RPC) ──► OpticStudioMCPServer ──ZOS-API──► Lens Data Editor
      │                                          zemax_set_surface_parameter
      │                                          zemax_set_surface (thickness/radius…)
      │                                          zemax_set_surface_solve (Pickup=跟随)
      │  ◄──────────────────────────────────────  zemax_get_merit_function
      ▼  y = {"merit": 0.0163, "rms_spot": 0.0163}
optplat 算法 (tell)
```

* MCP 客户端：`optplat/mcp_client.py`，**纯标准库**（stdio 上的行分隔 JSON-RPC 2.0），不新增任何依赖。
* MCP 服务端：[OpticStudioMCPServer](https://github.com/zym1998year/OpticStudioMCPServer)（**MIT**），
  **独立进程**（.NET + ZOS-API，需 Windows），不链接进本项目 → 闭源约束不受影响。
* 离线开发/CI：`optplat/zemax_sim.py` 是一个**说同一套协议、同一批工具名**的假服务器，
  所以整条链路在没有 Zemax 的机器上也能跑测试（`tests/test_zemax.py`）。

---

## 3. 自变量：Coordinate Break 参数

OpticStudio 里 Coordinate Break 的 PARM 编号是固定的，平台内置了名字映射（`optplat/zemax.CB_PARAMS`）：

| 名字 | PARM | 含义 |
|---|---|---|
| `decenter_x` | 1 | Decenter X |
| `decenter_y` | 2 | Decenter Y |
| `tilt_x` | 3 | Tilt About X |
| `tilt_y` | 4 | Tilt About Y |
| `tilt_z` | 5 | Tilt About Z |
| `order` | 6 | Order（0=先偏心后倾斜，1=反之） |

Thickness / Radius / Conic / Semi-Diameter 不是 PARM，用 `kind="thickness"` 等，走 `zemax_set_surface`。

```python
from optplat.zemax import ZemaxKnob, Follower

ZemaxKnob(surface=3, param="decenter_x", low=-1, high=1)          # 第 3 面 CB 的 Decenter X
ZemaxKnob(surface=3, kind="thickness", low=0, high=5)             # 第 3 面的 Thickness
ZemaxKnob(surface=3, param="decenter_x", scale=0.001)             # 平台用 µm，Zemax 用 mm
```

`zemax_value = scale * x + offset`，所以**平台单位和 Zemax 单位可以不一样**（µm↔mm、mrad↔deg 都靠它）。

### 跟随的 Coordinate Break（tilt/decenter-and-return）

一个 knob 可以带若干 `Follower`，`follower_value = scale * master_value + offset`，默认 `scale=-1`
（就是"倾斜/偏心后再还原"的标准写法）：

```python
ZemaxKnob(
    surface=3, param="tilt_x", low=-1, high=1,
    followers=[Follower(surface=5, param="tilt_x", scale=-1.0, mode="write")],
)
```

两种实现，按需要选：

| mode | 谁维护跟随关系 | 什么时候用 |
|---|---|---|
| `write`（默认） | 平台每次评估都写从属面 | 任何 OpticStudio 版本都行；从属值出现在平台自己的 history 里，好排查 |
| `pickup` | OpticStudio 的 **Pickup solve**（bind 时装一次） | 每次评估少几次写；注意 `pickupColumn` 编号随 OpticStudio 版本不同，必要时用 `pickup_column=` 显式指定 |

跟随不局限于同名参数、也不局限于一个面：`Follower(surface=7, param="decenter_y", scale=0.5)` 同样成立，
一个主动 knob 可以挂多个从属面。

---

## 4. 因变量：Merit Function Editor

```python
from optplat.zemax import MeritSpec, OperandRef

MeritSpec(
    total="merit",                                   # MFE 总评价函数值 → y["merit"]
    operands={
        "rms_spot": OperandRef(type="RSCE"),         # 按操作数类型取第一行
        "efl":      OperandRef(row=7),               # 或按 MFE 行号取
        "spot_c":   OperandRef(row=7, field="contribution"),   # value/contribution/target/weight
    },
)
```

一次评估只调用一次 `zemax_get_merit_function`（`includeValues=True` 会触发重算），所有 y 从同一次读数里取，
保证 x 与 y 严格对应。**评价函数是代价 → 目标默认 `minimize`**（`binding.build_vocs()` 会这么生成）。

约束照旧用平台自己的能力：`keep: "rms_spot < 0.02"`（保持 y1 再优 y2）、VOCS `constraints`、
stage 的 `stop.target`、全局 `until` ——都不需要改 Zemax 那边的评价函数。

---

## 5. 完整示例

```python
from optplat import Orchestrator
from optplat.zemax import (Follower, MeritSpec, OperandRef,
                           ZemaxBinding, ZemaxConnection, ZemaxEvaluator, ZemaxKnob)

def knob(p, lo, hi):
    return ZemaxKnob(surface=3, param=p, low=lo, high=hi,
                     followers=[Follower(surface=5, param=p, scale=-1.0)])

binding = ZemaxBinding(
    knobs={"dec_x": knob("decenter_x", -1, 1), "dec_y": knob("decenter_y", -1, 1),
           "tilt_x": knob("tilt_x", -1, 1),    "tilt_y": knob("tilt_y", -1, 1)},
    merit=MeritSpec(total="merit", operands={"rms_spot": OperandRef(type="RSCE")}),
)
connection = ZemaxConnection(
    command=[r"C:\…\OpticStudioMCPServer.exe"],
    mode="extension",            # 连已开着的 OpticStudio（需 Programming > Interactive Extension）
    file=r"C:\designs\lens.zmx",  # "standalone" 则是无界面新实例
)

vocs = binding.build_vocs()                       # 变量/目标直接由 binding 生成
ev = ZemaxEvaluator(binding, connection)
res = Orchestrator(vocs, ev, {
    "until": "merit < 0.02",
    "flow": [
        {"stage": "coarse", "algorithm": "grid_scan",
         "variables": ["dec_x", "dec_y"], "objective": "merit",
         "n_per_axis": 5, "stop": {"target": "merit < 0.6"}},
        {"stage": "align", "algorithm": "coordinate_descent",
         "variables": ["dec_x", "dec_y", "tilt_x", "tilt_y"], "objective": "merit",
         "step": 0.4, "stop": {"max_iter": 400}},
    ],
}, eval_budget=3000).run()
ev.close()
```

平台自己的 8 种算法（grid/line 扫描、坐标下降、Nelder-Mead、二次/高斯拟合、非标拟合、贝叶斯）全部可用，
也可以只把 Zemax 当"取值器"、让 `zemax_optimize` 那边的 LM 优化留给需要它的场合。

---

## 6. 与真实设备兼容 / 混合

同一份 IR，换 backend 即可从 Zemax 切到真机（`optplat/backends.py`）：

```jsonc
{"evaluator": {"mode": "hardware_sim", "noise": 0.01, "averages": 5, "safety": true}}
```

要**同时**跑（模型在环 + 硬件在环，例如用设计模型给实测标定提供参考量）：

```python
from optplat.zemax import CompositeEvaluator
comp = CompositeEvaluator({"zemax": (zemax_ev, ["dec_x", "dec_y"]),
                           "rig":   (hw_ev,    ["x1", "x2", "x3"])})
```

每个子 backend 只拿到自己那部分变量，y 合并返回（`prefix_outputs=True` 可加 `zemax.` / `rig.` 前缀防重名）。

**安全护栏在两边都生效**：`SafetyLimits` 在 Evaluator 层 clamp，Zemax 走的是同一段代码
（`EvaluatorConfig(safety=True)` 即按 VOCS 的 low/high 夹紧）。射线追迹撞不坏东西，但**同一份 binding
迟早会被指到真机上**，所以护栏留在同一个位置，不给"仿真时关掉、上机忘了开"留口子。

---

## 7. 已知边界

* `pickup` 模式的 `pickupColumn` 编号依赖 OpticStudio 版本；默认用 PARM 号，不对就显式传 `pickup_column`。
  拿不准就用默认的 `write` 模式。
* 只对接了 `zemax_connect / zemax_open_file / zemax_set_surface / zemax_set_surface_parameter /
  zemax_set_surface_solve / zemax_get_merit_function`。该服务器还有 spot/MTF/POP 等分析工具，
  要把分析结果当 y 的话，在 `ZemaxEvaluator._read()` 里加一条读取即可（结构已经留好）。
* Multi-configuration（MCE）、多重构型跟随尚未映射。
* 真机联调（Windows + OpticStudio + 真实 .zmx）尚未做过——离线链路全绿，但**面号、PARM 号、MFE 行号要按
  客户实际设计核对一遍**。
