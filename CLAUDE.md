# CLAUDE.md — 项目上下文与继续指南

> 这份文件是给 Claude Code 新会话的"接手说明"。读完它 + `ARCHITECTURE.md` 就能无缝继续。
> 面向的是一个**已经能跑的平台**，不是从零开始。改动前先跑 `python -m pytest -q`（应 23 项全过）。

---

## 1. 项目意图（用户的原始目标）

做一个**优化算法平台**，让客户快速搭建自己的优化流程，主要用于**光器件耦合、光器件标定、WDL 打架均衡**等场景。核心诉求：

1. **接入不同自变量/因变量数目的优化函数** `y1,y2,... = f(x1,x2,...)`；目标可以是
   最大/最小化、到某个**目标值**（可能需建模反解）、或**按范围扫描**（产特性曲线）。
2. **可换算法**（坐标下降、贝叶斯、拟合定峰等）。
3. **可加约束**（如优化 y2 时保持 y1>k）。
4. **可接入非标优化函数/逻辑**（特殊场景的专属逻辑与拟合）。
5. 让**不懂代码的用户**也能用 → 最终要**托拉拽画布**（Dify 风格：拖节点连线生成 JSON，JSON 驱动运行）。

典型流程：**第一阶段找光**（grid/line 扫描到阈值）→ **第二阶段优化**（精调/拟合/贝叶斯，带约束）。

## 2. 硬约束（不可违背）

- **闭源自用/交付给客户** → **依赖必须全部 permissive（MIT/BSD/Apache）**。
- **绝不引入 GPL/LGPL/AGPL 或 fair-code**：已明确排除 **Badger(GPL-3.0)** 和 **n8n(Sustainable Use License)**。
- 新增依赖前**先核实 license**，permissive 才用，并登记进 `THIRD_PARTY_LICENSES.md`。
- 不放开源 LICENSE 文件（闭源，无 license = 保留所有权利）。
- 公司内部有 **GLM5.1** 资源，未来做自然语言→IR 的 Copilot（用它，不用外部 LLM）。

## 3. 架构一句话

**声明式 IR 是唯一真相**（VOCS + pipeline/graph JSON）；所有 UI（表单/画布/未来 Copilot）都只是它的编辑器；
执行引擎只认 IR。**算法用开源 wrapper，编排自研，安全护栏独立且不可关闭。** 详见 `ARCHITECTURE.md`。

分层与文件对应：

| 层 | 文件 | 说明 |
|---|---|---|
| 问题声明 VOCS | `optplat/vocs.py` | 变量/目标(max·min·target·scan)/约束，Pydantic |
| 算法 Generator（ask/tell） | `optplat/generators.py` | 自研：grid/line 扫描、坐标下降、Nelder-Mead、二次/高斯拟合、公式法(非标拟合)、三点解析 |
| 贝叶斯（OSS wrapper） | `optplat/bayes.py` | Optuna(MIT) TPE/GP，惰性导入 |
| 算法插件 registry | `optplat/registry.py` | 参数 schema + builder；**自定义算法零改动接入**；画布读它建面板 |
| 执行核（共享） | `optplat/engine.py` | `StageEngine`：跑一个 stage、条件求值、全局早停、预算熔断 |
| 编排（块式） | `optplat/orchestrator.py` | flow / if / loop{until,max_rounds} |
| 编排（图式，Dify） | `optplat/graph.py` | `GraphRunner` 直接跑 {nodes,edges}；分支=条件边，循环=回边(max_visits/max_steps 限幅)；`to_mermaid` |
| 评估接入 | `optplat/evaluator.py` `optplat/hardware.py` | 函数版 + 硬件版(稳定时间/平均/**独立安全限位** clamp)；含仿真 stage/meter；按通道选择性读取+成本 |
| 模型接口 | `optplat/models.py` | ModelProvider 注册式：`analytic`(解析 bench) / `dataset_idw`(用户数据 surrogate)，可插拔 |
| 自动调优 | `optplat/autotune.py` | AutoTuner(L1)：候选生成(粗调×精调×拟合+参数/噪声变异)、多试验评估、质量/时长/稳定打分、帕累托 |
| 持久化 | `optplat/store.py` | SQLite 归档 / 断点续跑(start_point) / 一键回滚(rollback_to_best) |
| 后端 API | `optplat/api.py` | FastAPI：`/catalog` `/vocs` `/run/graph` `/run/pipeline` `/`(画布) |
| 托拉拽画布 | `web/index.html` | **纯 vanilla JS+SVG，无 CDN，离线可用**；产 {nodes,edges} JSON |
| 表单 UI | `app.py` | Streamlit 交互控制台（早期 MVP 面） |
| 仿真台/示例 | `optplat/demo.py` | `optical_bench` 模拟光耦合；`TWO_PHASE_*` 示例流程 |

## 4. 当前状态（已完成，全部有测试）

- **P0 MVP**：VOCS + 坐标下降 + 拟合(R²守门/外推限幅/回退) + if/loop/until 编排 + keep 约束 + Streamlit + YAML。
- **P1a 算法库**：grid/line 扫描、Nelder-Mead、公式法；两阶段"找光→优化"。
- **P1b**：Optuna 贝叶斯 wrapper；硬件适配器(噪声/平均/**安全限位**) + 仿真；SQLite 归档/续跑/回滚。
  （含修复：全局 `until` 中途触发时同步操作点，保证 state 与 objectives 一致。）
- **P1c 图运行时 + 插件**：node+edge 图 IR + `GraphRunner`（分支/受限循环）；算法插件 registry；graph→mermaid。
- **P2 服务化 + 画布**：FastAPI 后端；vanilla JS 托拉拽画布（served at `/`）。
- **P2a 画布易用性**（已浏览器可视化验证，Chromium 无控制台报错）：
  - 目标函数**轨迹图**（全部 / 间隔 N 点看收敛）；
  - 节点**目标模式**可配（最大/最小/逼近目标值/扫描，引擎按 `objective_mode`/`objective_target` 覆盖 VOCS 默认）；
  - **字号**滑块（缩放画布字体）；**连线**改进（DOM 精准锚点 + 橡皮筋预览 + 加宽点击区）与**删除**（选中删/`Delete`/右键）；
  - **帮助**说明文档弹窗 + 关键处 ⓘ 悬浮提示；算法节点显示**中文名**（registry 加 `label`/`desc`，参数带中文 `label`，`name` 仍为英文 ID）。
- **P2b 画布进阶**（Chromium 全流程验证，无控制台报错）：
  - **轨迹图三视图**：收敛(目标/变量 vs 评估次数，下方**算子色带**标注每段属于哪个算子) / **1D**(单自变量→目标) / **2D**(双自变量,颜色=目标值,等高线式,带 colorbar)；**下载轨迹 CSV**(全部 x/y)。
  - **多仿真场景**：`demo.py` 加 `BENCHES`(单峰/多峰/偏斜峰) + `bench_func`；`api` 加 `/benches`、`EvaluatorConfig.bench`；画布可选场景。
  - **逐变量安全限位**：`EvaluatorConfig.safety_limits` 覆盖；勾选后画布逐自变量填 low/high。模拟硬件下有**接口映射**占位(自变量↔执行器 / 目标↔功率计)。
  - **节点配色 + 循环回边绕行布线**（回边/自环走下方，避免交叉）；**保存/加载方案 JSON**；**表格式目标条件**(until 构建器) + 原始表达式。
  - **更轻盈的浅色主题**(默认) + 深色切换；帮助补**可用表达式**说明(比较/and·or·not/abs·min·max/变量名)。
- **P2c 评估架构 + 算法/拟合增强**（Chromium 验证，pytest 37 项全过）：
  - **按通道选择性读取 + 成本模型**：`Objective` 加 `cost`(秒)/`device`/`param`（通道规范）；`Evaluator`/`HardwareEvaluator.evaluate(x,stage,channels)` 只读需要的通道；`StageEngine._needed_channels`＝目标∪keep∪stop∪until 引用的目标——**只优化 y1 的步骤不读 y2**；结果返回 `reads`/`sim_seconds`；画布显示测量耗时/读取次数，通道规范可编辑(设备/参数/耗时)，`channel_costs` 可覆盖。
  - **经典测试函数**：`demo.py` 加 Rosenbrock(相关谷)/Rastrigin/Ackley(多峰)；`POST /surface` 采样响应面；画布 2D 视图可**叠加响应面等高线**(纯函数场景)。
  - **梯度上升(PI闪电式)** `gradient_ascent`：有限差分测局部梯度、沿上升方向步进+步长自适应。
  - **拟合公式回显**：`SurrogateFit`/`FormulaMethod`/`ParametricFit` 暴露 `fit_info`（峰位/参数/R²），引擎收进 `result.fits`（用 finally 保证全局早停也记录），画布对应**节点卡片显示拟合公式**。
  - **按场景示例 + 方案库**：`载入示例` 按当前仿真场景放量身流程（多峰/多模用贝叶斯全局、相关谷用单纯形、偏斜/尖峰用梯度上升等）；`📁 方案库` 内置各场景示例，并可选**整个文件夹批量加载**保存过的方案（webkitdirectory / 多选文件）。
- **测试**：`python -m pytest -q` → **37 项全过**（algorithms / graph / p1b / api）。

算法库（10 种，均 ask/tell、可在画布/图/块里用）：`grid_scan` `line_scan` `coordinate_descent`
`nelder_mead` `gradient_ascent`(PI闪电式) `quadratic_fit` `gaussian_fit` `parametric_fit`(非标拟合/公式法) `formula` `bayesian`。

- **P3 Agent/AutoTuner（Phase A 已做）**：见 `AGENT_AUTOTUNE_DESIGN.md`。
  - **L1 AutoTuner**（`optplat/autotune.py`）：在"粗调(网格/线扫/**贝叶斯**)×精调(单纯形/坐标/梯度)×拟合"三相空间搜索，
    变异集＝参数变异(含拟合/解析参数)+噪声变异；多试验按**质量/时长(sim_seconds)/稳定性(达标率或重复性)**打分、排名、帕累托；`POST /autotune`；画布 **🤖 自动调优** 面板(三权重滑块+帕累托散点+候选卡片+一键采用)。
    - **变异 registry 驱动**：相/算法按 `category→phase` 自动分组、档位从参数 schema 派生（`phase_algorithms`/`default_variation`），新算法自动纳入；画布**变异表格**可勾选/编辑（`GET /autotune/space`、`TuneSpec.variation`）。
    - **帕累托 2D 可选轴**（质量/时长/稳定/评估次数任选两轴，画非支配前沿）。
  - **加权组合单目标**（`StageEngine._scorer` + 节点 `objective_weights`）：单目标算子可优化 `Σ w·目标`(各目标按自身 mode 计分)。
  - **模型接口**（`optplat/models.py`）：ModelProvider 可插拔，`analytic`/`dataset_idw`，为客户采集数据建模留口。
  - **L2 MCP / L3 GLM5.1 副驾**：设计已定，待做（MCP SDK=MIT）。
- **pytest 48 项全过**（algorithms/graph/p1b/api/autotune）。

## 5. 路线图（下一步候选，未做）

- **P1d 多目标**：pymoo(Apache) NSGA-II wrapper（真帕累托前沿）；异步/批量 Evaluator（贝叶斯 batch、多通道并行）。
- **真实硬件**：把 `SimulatedStage/SimulatedMeter` 换成 **PyVISA/PyMeasure(MIT)** 封装的真实电机台/功率计
  `move(axis,value)`/`read()->dict`（按客户仪器型号写），其余不动。
- **GLM5.1 Copilot**：自然语言→IR（用 Instructor/Guardrails 做 schema 护栏），IR→人话解释/跑后诊断。
- **画布增强**：撤销/重排、保存/加载图 JSON、多目标帕累托可视化、`scan` mode 执行器（schema 已占位）。
- **服务化增强**：多用户/任务队列、把 VOCS 与 Evaluator 也做成可注册插件（目前 API 默认用 demo 光耦合台）。

## 6. 待办 / 待用户反馈的点（重要）

- **画布交互已用 Chromium 驱动验证**（拖拽/连线/删除/字号/目标模式/轨迹图/帮助/tooltip 均通过，无控制台报错）。
  但真机手感、连线视觉细节仍以**用户打开 `http://127.0.0.1:8000/` 的反馈**为准，按需微调。
- 用户会继续提需求（真实目标函数形态、WDL 均衡具体判据、仪器型号、界面细节）——按需迭代。

## 7. 关键决策与理由（别推翻，除非用户要求）

- **不用 Xopt/Badger 起步**：Xopt(Apache) 可接但学习成本高；Badger 是 GPL，**只可参考不可用**。保留 ask/tell 接口与其兼容。
- **不用 n8n**：fair-code，明文禁止嵌入产品给客户。要现成流程引擎的话 Node-RED(Apache) 才干净，但执行模型不匹配迭代优化+硬件在环，故**编排自研**。
- **公式法 = 非标拟合**：把已知参数钉死(lmfit vary=False)只解自由参数 + 支持自定义模型表达式(ExpressionModel)，不是固定的三点公式（`formula` 是其解析特例）。
- **画布用 vanilla JS 而非 React Flow**：闭源/内网/离线友好，无打包无 CDN；React Flow(MIT) 也可选，但当前无必要。
- **安全护栏独立**：loop 强制 `max_rounds`/`max_visits`、全局 `max_steps`/eval 预算、条件用 asteval 白名单、硬件安全限位在 Evaluator 层 clamp——任何算法/编排 bug 都不能命令越界运动。

## 8. 工作方式与约定

- **分支**：`claude/optimization-algorithm-platform-2z285n`（在此开发、提交、推送；勿推别的分支）。
- **每次改动**：跑 `python -m pytest -q` 确认不回归；新功能补测试；改依赖同步 `THIRD_PARTY_LICENSES.md` 并核实 license。
- **提交信息**：清晰描述改了什么、为什么、验证结果。
- **文档**：架构变化更新 `ARCHITECTURE.md`；使用方式更新 `DEMO.md`/`README.md`；重大意图/决策更新本文件。
- **不要**在推到仓库的产物里写入模型标识符。

## 9. 快速跑起来

```bash
pip install -r requirements.txt
python -m optplat.api      # 后端 + 画布 → http://127.0.0.1:8000/   （/docs 有 API 文档）
python run_graph.py        # 命令行跑图 JSON（Dify 风格）
python run_demo.py         # 命令行跑两阶段流程
python demo_hardware.py    # 硬件+噪声/平均/安全+续跑+回滚
streamlit run app.py       # 表单式控制台
python -m pytest -q        # 23 项测试
```

接自己的优化函数：改 `optplat/demo.py` 的 `optical_bench(x)` 与 `demo_vocs()`。
接自己的算法：实现 `Generator`(ask/tell) 后 `register_algorithm(AlgorithmSpec(...))`，即成为可拖拽节点。
