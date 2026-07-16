# AGENT_AUTOTUNE_DESIGN — 优化副驾(Copilot) + 方案自动调优(AutoTuner)

> 目标：让系统能**自动出一版方案 → 按质量/时长/稳定性搜索 → 找到最优算法方案**；
> 并能像 MCP 一样被外部 Agent（内部 GLM5.1）驱动。
> 本文是落地设计；实现按 L1 → L2 → L3 分层交付。**当前进度：L1(AutoTuner) Phase A 实现中。**

---

## 0. 核心洞察

"找最优算法方案" 本质是**外层优化**：在"工作流 IR(节点+连线)"空间里搜索，用多目标效用打分。
平台已能**确定性地给一个工作流打分**，无需 LLM：

| 维度 | 平台现成量 |
|---|---|
| 质量 quality | `result.objectives`（最终 y） |
| **时长 time** | `result.sim_seconds`（通道成本模型）+ `n_evals` |
| **稳定性 stability** | 跑多 seed/多噪声 → 最终值方差 & 达标率（噪声/平均已支持） |
| 安全 safety | 安全限位夹回次数 |

所以 **L1 = 确定性 AutoTuner**（闭源、依赖全 permissive、可测试），是整套的地基，也是后面 MCP 工具 `autotune` 的实现体。

---

## 1. 分层架构

- **L1 · AutoTuner（确定性内核，无 LLM）** ← 先做（Phase A）
- **L2 · MCP 服务器**：把 `list_algorithms / run_workflow / score_workflow / autotune / propose_workflow / explain_result` 暴露成 MCP 工具，任何 MCP 客户端（GLM5.1 agent、Claude…）可驱动。传输 stdio + HTTP/SSE，用官方 MCP Python SDK（MIT）。平台=server，agent=client；内部护栏绕不过。
- **L3 · Copilot（GLM5.1，可选）**：自然语言→IR 起草（Pydantic schema 护栏）+ 从措辞推权重（"要快"→时长权重高，"要稳"→稳定权重高）+ 人话解释。GLM5.1 只产出"过校验的 IR"，绝不直接命令运动。没接通时 L1+L2 照常工作。

---

## 2. L1 AutoTuner 详细设计（Phase A）

### 2.1 候选空间：三相流水线
一条候选工作流 = **粗调 → 精调 → 拟合(可选)** 三相链：

- **粗调 / 找光 coarse**：`grid_scan` `line_scan` **`bayesian`**（贝叶斯归此相）、或 `none`
- **精调 refine**：`nelder_mead` `coordinate_descent` `gradient_ascent`、或 `none`
- **拟合 / 定值 fit**（对第二目标，如 x3→y2）：`gaussian_fit` `quadratic_fit` `formula` `parametric_fit`、或 `none`

约束：至少有一相非 `none`。相到变量的映射由 `TuneSpec` 给（默认 demo：粗调/精调作用在 `x1,x2→y1`，拟合作用在 `x3→y2`）。

### 2.2 变异集（Variation Set）— 三个变异轴
候选 = 三相组合 **×** 下面三种变异：

1. **参数变异 param variation（registry 驱动，非写死）**：相/算法从 registry 按
   `category→phase` 自动分组（`find-light`/`bayesian`→粗调、`local`→精调、`fit`/`analytic`→拟合），
   **新算法只要声明 category 就自动进对应相**；每个算法的变异档位**从其参数 schema 自动派生**
   （默认+各参数极值），无需在 autotune 里写死。用户可在画布**变异表格**里勾选参与的算法、
   编辑每个参数的取值档位（`TuneSpec.variation` 覆盖）。`GET /autotune/space` 提供表格数据。
   —— 已实现：`phase_algorithms()` / `default_variation()` / 画布『变异表格』。

2. **噪声变异 noise variation**：在多个噪声档（如 `σ∈{0, 0.02, 0.05}`）× 多 seed 下重复跑，
   考察**稳定性/鲁棒性**——好方案要在噪声下仍达标、方差小。

3. **模型变异 model variation（多模型接口）**：评估函数不再写死为解析 bench，而是经
   **ModelProvider 接口**产生（见 §2.3）。这样后续"用户采集了很多数据 → 建模"可直接作为
   一个 model 源接入，参与同一套调优；不同建模方式（解析式/数据surrogate/未来的物理模型）都是可插拔的 provider。

### 2.3 模型接口（ModelProvider，多模型可插拔）
`optplat/models.py`：统一"给 x 出 y"的来源，注册式，和算法 registry 同构。

```python
# spec 决定用哪种模型：
{"kind": "analytic", "bench": "single_peak"}          # 解析式仿真台（现有 BENCHES）
{"kind": "dataset_idw", "data": [{x1,x2,x3,y1,y2}...], # 用户采集数据 → IDW 反距离加权 surrogate
 "objectives": ["y1","y2"], "variables": ["x1","x2","x3"]}
# 未来：{"kind":"gp"/"rbf"/"nn"/"physical", ...} 只需再注册一个 provider
build_model(spec) -> Callable[[dict[str,float]], dict[str,float]]
```

v1 落地：`analytic`（包 BENCHES）+ `dataset_idw`（numpy 反距离加权，permissive）。
接口固定后，客户拿真实采集数据建模只是新增一个 provider，AutoTuner/画布/评估层都不用改。

### 2.4 评估与打分
对每个候选：在 `noise_levels × n_trials(seed)` 下用 `GraphRunner` 跑（复用现有引擎与安全护栏），收集：
`final objectives / sim_seconds / n_evals / 是否达标(target) / 安全越界次数`。聚合：

- `quality` = 各 trial 最终"质量目标"均值（默认粗调目标，如 y1；可配 `quality_obj`）
- `time` = 平均 `sim_seconds`（+可选 `n_evals`）
- `stability` = 有 `target` 时的**达标率**；否则 `1 − 变异系数(quality)`
- `violations` = 平均安全越界次数（惩罚项）

标量化效用（各指标先在候选间归一化到 [0,1]）：
```
U = wQ·quality_n + wS·stability_n − wT·time_n − wV·violations_n
```
同时给出 **帕累托前沿**（质量↑ / 时长↓ / 稳定↑），避免被单一权重绑死。按 U 排名。

### 2.5 搜索策略
- **Phase A**：在精选候选表上**穷举/采样**（`max_candidates` 限幅，去重）。
- **Phase A+（可选）**：用平台自己的 `bayesian` 在"候选模板 × 参数变异"空间上搜（平台自我调优），受 `budget` 限幅。

### 2.6 数据结构
```python
class TuneSpec(BaseModel):
    bench: str = "single_peak"                 # 或 model: ModelSpec
    landscape_vars: list[str] = ["x1","x2"]; landscape_obj: str = "y1"
    balance_vars: list[str]  = ["x3"];       balance_obj: Optional[str] = "y2"
    target: Optional[str] = "y1>=0.95 and y2>=0.9"   # 达标定义（可空=纯最大化）
    quality_obj: Optional[str] = None          # 默认 landscape_obj
    weights: dict = {"quality":1.0, "time":0.5, "stability":1.0}
    noise_levels: list[float] = [0.0, 0.02]
    n_trials: int = 3
    max_candidates: int = 24
    eval_budget: int = 2000
    seed: int = 0
    allow: dict = {...}                        # 各相允许的算法（默认全开）
    param_variation: bool = True

class CandidateResult(BaseModel):
    id: str; label: str; graph: dict
    quality: float; time: float; stability: float; violations: float; evals: float
    utility: float; pareto: bool; detail: dict     # 逐档统计
```

### 2.7 API / 画布
- `POST /autotune` → `{ranked: [CandidateResult...], spec_echo}`；`GET /autotune/space`（变异表格数据）。
- 画布 **🤖 自动调优** 面板：三滑块（质量/速度/稳定）+ 噪声档 + 预算 + **变异表格**（勾选算法/编辑参数档位）→ 运行 →
  候选卡片（指标 + "采用"一键上画布）+ **帕累托散点，坐标轴可选**（质量/时长/稳定/评估次数任选两轴，画 2D 非支配前沿）。

### 2.8 加权组合单目标（composite objective）
单目标算子（单纯形/坐标/梯度/拟合）可优化**多目标的加权合成**：节点带 `objective_weights`
（如 `{"y1":0.7,"y2":0.3}`），引擎对每个目标**按其自身 mode(最大/最小/逼近)计分后加权求和**
作为该 stage 的标量分数；通道读取自动覆盖所有被引用目标。画布节点属性面板可勾『加权组合多目标』并逐目标填权重。
—— 已实现：`StageEngine._scorer`；画布 composite 权重编辑。

---

## 3. 合规
- 依赖仅 numpy/scipy（BSD）；MCP SDK（L2）为 MIT。均 permissive，登记 `THIRD_PARTY_LICENSES.md`。
- 无外部 LLM；L3 仅接内部 GLM5.1。全程 IR 驱动，安全护栏不变、不可绕过。

---

## 4. 交付节奏
- **Phase A（本次）**：`models.py`(ModelProvider) + `autotune.py`(候选生成/变异/评估/打分/帕累托) + `POST /autotune` + 画布面板 + 测试。
- **Phase B**：MCP server 包装工具。
- **Phase C**：GLM5.1 副驾（NL→IR + 权重推断 + 解释）。

## 5. 待定/迭代点
1. 默认权重（当前 质量1·时长0.5·稳定1，均衡偏质量）。
2. "达标"定义：默认给 `target` 表达式；纯最大化时用变异系数当稳定性。
3. 真机成本：建议"仿真海选 → Top-K 上真机确认"。
4. 数据建模：v1 提供 IDW surrogate；GP/RBF/物理模型后续按 provider 接入。
