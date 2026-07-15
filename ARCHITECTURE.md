# 最终架构方案 · 光器件优化算法平台

> 场景：光器件耦合 / 标定 / WDL 均衡等硬件在环优化。
> 商业约束：闭源交付给客户，依赖必须全为 permissive license（MIT/BSD/Apache），零 GPL/SUL 传染。
> 本文是全部架构讨论的最终收敛版，代码骨架已可运行（`python run_demo.py` / `streamlit run app.py`）。

---

## 0. 三条不可动摇的架构公理

1. **声明式 IR 是唯一真相（Single Source of Truth）**
   问题定义（VOCS）+ 编排流水线（pipeline）= 一份带 JSON Schema 的声明式配置。
   表单、托拉拽画布、GLM5.1 Copilot 都只是这份 IR 的"编辑器"；执行引擎只认 IR，永不认 UI。
   → 三种入口可互相翻译、随时切换；配置天然可版本化、可复现、可归档。

2. **算法层统一 ask/tell 接口，算法尽量用开源，编排必须自研**
   `ask()` 给下一批候选点，`tell(x, score)` 回填结果——算法完全不知道 f 是仿真还是真实电机。
   贝叶斯/多目标这类难写易错的算法从开源接（Optuna/skopt/Xopt/pymoo，全 permissive）；
   编排状态机（if/loop/keep/守门/回退）是全平台唯一没有开源对应物的部分，也是差异化核心，自研且保持薄。

3. **安全护栏独立于算法与编排，且不可关闭**
   受限 DSL（非图灵完备）：loop 强制 `max_rounds`、stage 强制 `max_iter`、全局评估预算熔断；
   条件表达式白名单求值（asteval），不执行任意代码；
   硬件安全限位在 Evaluator 层独立实现，任何算法/编排都不能突破。
   LLM 只生成"候选配置"，必须过 schema 校验 + 用户确认才执行，绝不直接触发硬件动作。

---

## 1. 分层总图

```mermaid
flowchart TB
    subgraph UI["用户面（三个编辑器，不写算法代码）"]
      direction LR
      FORM["表单/模板向导<br/>rjsf · Apache"]
      CANVAS["托拉拽画布<br/>React Flow · MIT"]
      NL["GLM5.1 Copilot<br/>Instructor · MIT"]
    end

    IR["★ 声明式 IR（JSON Schema 校验）★<br/>VOCS: 变量/目标(max·min·target·scan)/约束<br/>pipeline: stage · if · loop{until,max_rounds} · keep · fallback · 全局until"]

    ORCH["编排层 Orchestrator（自研，受限状态机）<br/>顺序/分支/受限循环/阶段停机/keep约束/拟合守门回退/全局早停/预算熔断"]

    subgraph GEN["算法层 Generator（统一 ask/tell）"]
      direction LR
      SELF["自研：坐标梯度 · 拟合定峰<br/>(二次/高斯, R²门+外推限幅, lmfit·BSD)"]
      OSS["开源 wrapper：贝叶斯(skopt·BSD / Optuna·MIT)<br/>多目标(pymoo·Apache) · Xopt·Apache"]
    end

    subgraph EVAL["评估接入层 Evaluator"]
      direction LR
      FN["Python 函数适配器"]
      HW["硬件适配器 PyVISA/PyMeasure·MIT<br/>稳定时间/平均/迟滞 + 独立安全限位"]
      HOOK["非标逻辑 Hook<br/>pre_move / post_measure / on_stage_end"]
    end

    DATA["运行时与数据层<br/>全量归档(SQLite/parquet) · 断点续跑 · 一键回滚最优点 · 实时收敛曲线/帕累托前沿"]

    DEV["被优化对象：耦合台 / 标定台 / WDL 均衡（或仿真台）"]

    UI <-->|双向：生成/渲染/解释| IR
    IR --> ORCH
    ORCH -->|按 stage 实例化| GEN
    GEN -->|ask: 下一组 x| EVAL
    EVAL -->|tell: y·约束观测| GEN
    EVAL --> DEV
    ORCH --> DATA
    EVAL --> DATA
    DATA -->|曲线/轨迹/诊断| UI
```

## 2. 各层最终决策

### 2.1 声明层 VOCS（已实现 `optplat/vocs.py`）
- 变量：任意个数，范围 + 可选分辨率；目标：任意个数，四种 mode 统一
  （maximize / minimize / **target**（转 `-|y-t|` 或代理模型反解）/ **scan**（退化为扫描生成器，产特性曲线））。
- 约束两类：**硬约束**（提点时过滤，如安全区）与**软约束/keep**（违反则罚分+回退，如 `y1 > k`）。
- Pydantic 建模 → 自动导出 JSON Schema，同一份 schema 驱动表单生成、LLM 输出校验、后端校验（三处一源）。

### 2.2 算法层 Generator（已实现 6 种 + wrapper 路线）
- 统一 `ask()/tell()/done/failed/best_x`，与 Xopt 接口兼容——将来可直接挂它的 generator。
- **已实现算法库**（`optplat/generators.py`，全部 ask/tell，仅依赖 numpy/scipy/lmfit）：

  | algorithm | 类别 | 说明 |
  |---|---|---|
  | `grid_scan` / `line_scan` | 找光 (Phase 1) | 栅格/线扫描，配 stage `stop.target` 到阈值即停 |
  | `coordinate_descent` | 局部优化 | compass search + 步长收缩 |
  | `nelder_mead` | 局部优化 | 单纯形下降（reflect/expand/contract/shrink，ask/tell 驱动） |
  | `quadratic_fit` / `gaussian_fit` | 标准拟合定峰 | 最小二乘（lmfit）全自由拟合，**R² 守门 + 外推限幅**，score 空间统一覆盖 max/min/target |
  | `parametric_fit` (公式法/非标拟合) | 带先验的拟合 | 把**已知参数钉死**（顶点/σ/曲率），只解自由参数；支持**自定义模型表达式**（客户非标公式）；参数钉够即退化成"公式"。lmfit `vary=False` + `ExpressionModel` |
  | `formula` | 解析特例 | 三点抛物线闭式解峰，**无回归**（= 参数全被点数定死的 parametric_fit 特例） |

- **其余 wrapper 接开源**：贝叶斯→scikit-optimize(BSD)/Optuna(MIT)；多目标帕累托→pymoo(Apache)；
  更多无梯度→Nevergrad(MIT)；成熟组合→Xopt(Apache)。每个 wrapper ≈ 几十行。
- 客户自定义算法 = 实现同一基类，entry_points 注册为插件。

### 2.3 编排层 Orchestrator（已实现 `optplat/orchestrator.py`，自研核心）
- 模型：**受限状态机**（不是 DAG——优化需要回边循环；不是通用脚本——硬件不允许图灵完备）。
- 算子集固定五个：`stage` / `if-then-else` / `loop{body, until, max_rounds}` / stage 级 `stop{max_iter, target}` / pipeline 级全局 `until`（任意时刻满足即整体早停）。
- stage 语义：冻结其余变量，只把本 stage 的变量子集交给 generator；结束时把操作点移到最优可行点。
- `keep` 约束：违反罚分（后续升级为贝叶斯的可行性感知采集函数）；`fallback`：拟合守门失败自动降级坐标梯度。
- YAML 嵌套块 ↔ 画布控制节点是同一状态机的两种同构视图。

### 2.4 评估接入层 Evaluator（函数版已实现，硬件版 P1）
- 契约：`evaluate(dict[x]) -> dict[y]`，自变量/因变量数目任意；全量历史自动归档。
- 硬件适配器：PyVISA/PyMeasure 通信 + 你的电机台/功率计驱动 + 稳定时间/平均/迟滞补偿；
  **安全限位在这一层独立实现**。非标场景逻辑通过 Hook 切点注入，不改平台代码。
- P1 加异步/批量接口（贝叶斯 batch 采样与多通道并行需要）。

### 2.5 用户面（三个编辑器）+ GLM5.1 定位
- **模板库优先**：「首光→定峰」「WDL 多通道均衡」「标定扫描」预置流程，客户选模板填 3~5 个旋钮
  （拟合 R² 阈值、信赖域半径、采样图案、平均次数都做成模板默认值+高级项）。行业 know-how 沉淀在这，是付费点。
- 表单由 JSON Schema 自动生成（rjsf）；画布用 React Flow(MIT)，节点↔IR 双向绑定；
- GLM5.1 三个用法：**意图→IR**（Instructor 结构化输出，schema 不合规自动重试）、**IR→人话解释+体检**、**跑后诊断**（读归档数据给建议）。边界：LLM 永远只产候选配置，经"渲染→校验→用户确认"才执行。

### 2.6 运行时与数据层（MVP 内存版 → P1 持久化）
- SQLite/parquet 归档每次评估（x, y, 时间戳, stage）；断点续跑；异常/中止时**一键回滚电机到历史最优点**；
  实时收敛曲线、多目标时帕累托前沿；归档数据可直接喂代理模型（target/建模类任务复用）。

## 3. License 红黑榜（已核实）

| 结论 | 组件 | License |
|---|---|---|
| 🔴 禁用 | Badger（编排+GUI 思路可参考，代码不可碰） | GPL-3.0 |
| 🔴 禁用 | n8n（明文禁止嵌入产品/向客户提供） | Sustainable Use License |
| 🟡 可用但不选 | Node-RED（license 干净，但消息流模型不匹配迭代优化+硬件在环，且 Node.js 与 Python 算法割裂） | Apache-2.0 |
| ✅ 采用 | Xopt · pymoo · Streamlit · rjsf | Apache-2.0 |
| ✅ 采用 | Optuna · Nevergrad · React Flow · Instructor · PyVISA · asteval · pydantic · pyyaml | MIT |
| ✅ 采用 | scipy · numpy · lmfit · scikit-optimize · pandas | BSD-3 |

本仓库不放 LICENSE 文件：闭源产品，无 license = 保留所有权利。

## 4. 自研 vs 开源 最终分界

**自研（护城河，约 20% 工作量）**：编排状态机 + 受限 DSL、拟合定峰家族、光器件模板库、GLM5.1 Copilot 集成、非标硬件适配器。
**开源装配（约 80%）**：全部通用算法、拟合底层、表单/画布 UI 组件、LLM 结构化输出、仪器通信。

## 5. 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0 · MVP** | VOCS + 坐标梯度 + 拟合定峰(R²门/限幅/回退) + if/loop/until 编排 + keep 约束 + Streamlit + YAML IR | ✅ 已完成并跑通 |
| **P1a · 算法库** | 找光扫描(grid/line)、Nelder-Mead、公式法(三点解析)；两阶段"找光→优化"流程；回归测试 | ✅ 已完成（6 种算法，两阶段 62 次评估收敛，5 tests 通过） |
| **P1b · 硬件+持久化** | skopt/Optuna 贝叶斯 wrapper、pymoo 多目标、异步/批量 Evaluator、PyVISA 硬件适配器 + 安全限位、SQLite 归档/续跑/回滚 | 下一步 |
| **P2 · 易用性** | JSON Schema 正式化 + rjsf 表单、场景模板库、GLM5.1 Copilot（意图→IR / IR→人话 / 跑后诊断） | |
| **P3 · 平台化** | React Flow 画布（节点↔IR 双向）、FastAPI 服务化 + 多用户/任务队列、scan 模式与建模类任务闭环 | |

## 6. 已知简化与升级路径（诚实清单）

- keep 约束目前是 `-1e9` 罚分 → P1 升级为可行性感知（贝叶斯 constrained acquisition / 回退到最近可行点）。
- 编排是树遍历执行器（够用）→ 需要 `goto/on_fail` 跨节点跳转时升级为显式状态机表（`transitions`·MIT）。
- Evaluator 同步单点 → P1 加 async/batch。
- 历史在内存 → P1 落 SQLite/parquet。
- 多目标目前靠"分阶段+keep"标量化 → 真帕累托需求出现时接 pymoo。
- `scan` mode 已在 schema 中占位，执行器 P3 实现。
