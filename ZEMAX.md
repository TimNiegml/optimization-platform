# 接入 Zemax OpticStudio（经 OpticStudioMCPServer）

> 目标：自变量 = 某个 **Coordinate Break** 的 Decenter X/Y、Thickness、Tilt About X/Y/Z（**跟随的
> Coordinate Break 自动同步**）；因变量 y = **Merit Function Editor** 的值。
> 同时**不影响真实设备接入**——Zemax 只是众多 backend 之一，可与仪器混跑。

```bash
python run_zemax.py                                   # 离线：内置假 OpticStudio 服务器，直接能跑
python run_zemax.py "C:/…/OpticStudioMCPServer.exe" "C:/designs/lens.zmx"   # 真机
python -m optplat.zemax_inspect "C:/…/Server.exe" "C:/designs/lens.zmx"     # 只看有哪些可选变量
```

**自变量不用手写面号/PARM 号**：平台读取 `.zmx`，把每个面能调的量列成清单让你勾（§3.1）；
设计里已有的跟随关系（Pickup solve）自动带出来（§3.3）；自变量一变，伴随变量/系统/光瞳按需刷新并**回读校验**（§5）。

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

## 3. 自变量：读设计 → 选面 → 选变量

### 3.1 平台读 `.zmx`，把可选变量列出来

不需要事先知道"第 3 面 PARM 3"。`optplat/zemax_inspect.py` 把 Lens Data Editor 读成一份快照，
再把每个面能调的量列成**可勾选清单**（带类型、备注、单位、当前值、已有的跟随关系）：

```bash
$ python -m optplat.zemax_inspect "C:/…/Server.exe" "C:/designs/lens.zmx"
fake design · 7 面 · mm · 1 视场 · 1 波长

可选自变量：
  s3.decenter_x     S3 · align in · Decenter X      当前=0     [mm]   跟随→ 面5(×-1)
  s3.tilt_x         S3 · align in · Tilt About X    当前=0     [deg]  跟随→ 面5(×-1)
  s2.thickness      S2 · lens front · Thickness     当前=3     [mm]
  …
可选目标（Merit Function Editor）：
  行2 · RSCE        value=0.324325   target=0 weight=1
```

代码里：

```python
snap    = inspect_system(client)      # SystemSnapshot：面/类型/备注/PARM 值/solve
choices = knob_choices(snap)          # 可勾选清单（KnobChoice）
opts    = operand_choices(snap)       # MFE 行，作为目标的候选
```

画布上（`web/index.html` → 评估方式选 **Zemax OpticStudio**）就是这条链路的 UI：
填 MCP 服务器路径和 `.zmx` → **读取系统** → 按面分组勾选参数、填范围 → 选目标（总评价值或某一行操作数）。
勾完的变量立刻成为算法节点里可选的自变量，运行时随图 JSON 一起发给 `/run/graph`。

对应 API（画布用的就是它，也可自己调）：

| 端点 | 作用 |
|---|---|
| `POST /zemax/inspect` | `{connection}` → 面清单 + 可选变量 + MFE 行 |
| `POST /zemax/binding` | `{connection, selections, merit}` → 可直接执行的 `binding` + `vocs` JSON |

```python
binding = build_binding(snap, [
    {"name": "tilt_x", "surface": 3, "param": "tilt_x", "low": -1, "high": 1},
    {"name": "dec_x",  "surface": 3, "param": "decenter_x", "low": -1, "high": 1},
], merit=MeritSpec(total="merit", operands={"rms_spot": OperandRef(type="RSCE")}))
vocs = binding.build_vocs()           # 变量/目标一并生成
```

### 3.2 Coordinate Break 参数编号

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

### 3.3 跟随的 Coordinate Break（tilt/decenter-and-return）

**优先做法：不配置，靠发现。** 设计里如果已经用 Pickup solve 写好了"倾斜后还原"，
`detect_followers()` 会扫描全系统的 solve，把指向所选单元格的面连同设计自己的 scale/offset 一起挂上——
选了主动面，伴随面自动跟着，`build_binding()` / `/zemax/inspect` 都已经这么做了。

> 匹配规则：先按 `pickupColumn` 精确匹配；一个都匹配不上时，才退回"同一 PARM 序号"
> （不同 OpticStudio 版本的列编号约定不同，常数偏移下同序号关系不变）。两种规则不会同时生效——
> 宁可少认，也不凭空造出并不存在的伴随关系。

也可以手写（设计里没做 pickup、或想临时改跟随系数时）。一个 knob 可以带若干 `Follower`，
`follower_value = scale * master_value + offset`，默认 `scale=-1`（就是"倾斜/偏心后再还原"的标准写法）：

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
一个主动 knob 可以挂多个从属面；`kind="thickness"` 的跟随（气隙跟着走）也支持。

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

## 5. 自变量一变，什么必须刷新

写一个 Coordinate Break 参数，会让它下游的一切失效：伴随面、光线追迹、（带 Ray Aiming 的设计里）光瞳。
每次评估的顺序是固定的：

```
写 x（含 write 模式的伴随面）
   → 刷新系统（可选，应用 solve / 取光瞳状态）
   → 读 Merit Function ← 这一步是权威重算：OpticStudio 在算评价函数前会把系统更新到最新
   → 回读伴随面，确认真的跟上了
```

**顺序本身就保证了 y 对应的是最新的 x**（评价函数不会拿旧系统去算）。`RefreshSpec` 管的是顺序管不到的部分：

```python
ZemaxBinding(knobs=..., merit=..., refresh=RefreshSpec(
    system=True,       # 写完后显式读一次系统：pickup solve 等在读任何东西之前先被应用
    pupil=False,       # 顺带取孔径/光瞳状态（Ray Aiming 设计里光瞳是解出来的）→ ev.last_pupil
    followers=True,    # 回读伴随单元格，核对是否真的跟随（默认开）
    tolerance=1e-6,
))
```

`followers=True` 是这里最值钱的一条：**pickup 列号在你这版 OpticStudio 上不对、或者 `.zmx` 里
那条 solve 被人删了，会立刻抛 `FollowerDesync` 报错，而不是拿一个"返回面没跟上"的错设计闷头优化几千步。**
回读值同时写进平台自己的 history（`s5.p3` 这样的键），和 x、y 并排存进 SQLite，事后可查：

```
final操作点: {'dec_x': 0.125, 'dec_y': -0.0781, 'tilt_x': 0.2969, 'tilt_y': -0.2031}
伴随变量(回读确认): {'s5.p1': -0.125, 's5.p2': 0.0781, 's5.p3': -0.2969, 's5.p4': 0.2031}
```

代价是每次评估多几个 MCP 往返（系统刷新 1 次 + 每个伴随面 1 次）。追速度可以关：
`RefreshSpec(system=False, followers=False)`——**先跑通、确认跟随正确，再关**。

---

## 6. 完整示例（读设计 → 选变量 → 优化）

完整可跑版本见 `run_zemax.py`。

```python
from optplat import Orchestrator
from optplat.zemax import (MeritSpec, OperandRef, RefreshSpec,
                           ZemaxBinding, ZemaxConnection, ZemaxEvaluator)
from optplat.zemax_inspect import build_binding, inspect_system, knob_choices

connection = ZemaxConnection(
    command=[r"C:\…\OpticStudioMCPServer.exe"],
    mode="extension",            # 连已开着的 OpticStudio（需 Programming > Interactive Extension）
    file=r"C:\designs\lens.zmx",  # "standalone" 则是无界面新实例
)

# 1) 读设计：开一个会话，把 Lens Data Editor / MFE 读出来
session = ZemaxEvaluator(ZemaxBinding(), connection)
session.bind()
snap = inspect_system(session.client)
for c in knob_choices(snap):
    print(c.id(), c.label, c.current, c.unit, [f.surface for f in c.followers])

# 2) 选变量：面 + 参数 + 范围（伴随面来自设计里的 pickup solve，不用写）
binding = build_binding(snap, [
    {"name": "dec_x",  "surface": 3, "param": "decenter_x", "low": -1, "high": 1},
    {"name": "dec_y",  "surface": 3, "param": "decenter_y", "low": -1, "high": 1},
    {"name": "tilt_x", "surface": 3, "param": "tilt_x",     "low": -1, "high": 1},
    {"name": "tilt_y", "surface": 3, "param": "tilt_y",     "low": -1, "high": 1},
], merit=MeritSpec(total="merit", operands={"rms_spot": OperandRef(type="RSCE")}))
binding.refresh = RefreshSpec(system=True, followers=True, pupil=True)

# 3) 优化
vocs = binding.build_vocs()                       # 变量/目标直接由 binding 生成
ev = ZemaxEvaluator(binding, connection, client=session.client)
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
print(ev.last_followers)                          # 伴随面回读值，已核对过
session.close()
```

平台自己的 8 种算法（grid/line 扫描、坐标下降、Nelder-Mead、二次/高斯拟合、非标拟合、贝叶斯）全部可用，
也可以只把 Zemax 当"取值器"、让 `zemax_optimize` 那边的 LM 优化留给需要它的场合。

---

## 7. 与真实设备兼容 / 混合

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

## 8. 已知边界

* `pickup` 模式的 `pickupColumn` 编号依赖 OpticStudio 版本；默认用 PARM 号，不对就显式传 `pickup_column`。
  拿不准就用默认的 `write` 模式——而且把 `RefreshSpec.followers` 开着，编号错了会直接报错。
* 只对接了 `zemax_connect / zemax_open_file / zemax_get_system / zemax_get_surface /
  zemax_get_surface_solves / zemax_set_surface / zemax_set_surface_parameter /
  zemax_set_surface_solve / zemax_get_merit_function`。该服务器还有 spot/MTF/POP 等分析工具，
  要把分析结果当 y 的话，在 `ZemaxEvaluator._read()` 里加一条读取即可（结构已经留好）。
* 自动发现的跟随关系只认 **Pickup solve**。用 MCE 多重构型、ZPL 宏或 Extra Data 实现的联动读不出来，
  需要手写 `Follower`；Multi-configuration（MCE）本身尚未映射。
* 光瞳刷新目前是"读回孔径/光瞳状态并记录"（`ev.last_pupil`）。Ray Aiming 的开关/模式没有在每次评估里改动——
  设计里怎么设的就怎么用（`zemax_set_ray_aiming` 该服务器有，需要的话可加进 bind 流程）。
* 真机联调（Windows + OpticStudio + 真实 .zmx）尚未做过——离线链路全绿，但**面号、PARM 号、MFE 行号要按
  客户实际设计核对一遍**。
