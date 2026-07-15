# DEMO 上手指南

三种方式体验平台，从零依赖到交互界面。先装依赖：

```bash
pip install -r requirements.txt
```

---

## A. 交互界面（推荐先玩这个）

```bash
streamlit run app.py
```

浏览器打开后，**左侧控制台**可以调：

1. **工作流**：
   - `两阶段：找光 → 精调 → 均衡` —— 你描述的典型流程（grid 找光到阈值 → Nelder-Mead 精调 → 公式法均衡）
   - `交替循环：拟合 + keep 约束 + 回退` —— 演示 `loop/until`、`keep y1>0.8`、拟合失败自动降级
   - `自定义单步` —— 挑任意一个算法单独试（8 种任选），改采样点数 / R² 门 / 贝叶斯预算等
2. **变量范围**：拖滑块改 x1/x2/x3 的范围
3. **评估方式**：
   - `纯函数` —— 快
   - `模拟硬件` —— 打开后可调 **测量噪声 σ**、**每点平均次数**、**安全限位**（看噪声下平均如何稳住结果）
4. **归档到 SQLite**：勾上后结果落库

点 **▶ 运行优化**，右侧看：指标（y1/y2/评估次数）、**实时收敛曲线**、最优操作点、**编排轨迹**（每一步在做什么）、完整评估历史表。

**建议试试**：
- 选「自定义单步」→ 算法 `bayesian` → 变量 x1,x2 → 看贝叶斯如何逼近峰。
- 选「自定义单步」→ 算法 `parametric_fit` → 变量 x3 → 勾「钉死顶点」→ 体验非标拟合/公式法。
- 打开「模拟硬件」，把噪声 σ 拉到 0.05，平均次数从 1 调到 10，看曲线抖动如何被压下去。

---

## B. 命令行：两阶段流程

```bash
python run_demo.py          # 两阶段：找光 → 精调 → 均衡
python run_demo.py loop     # 交替循环 + keep 约束 + 回退
```

打印完整编排轨迹和最终最优点。

---

## C. 命令行：硬件接入 + 断点续跑 + 一键回滚

```bash
python demo_hardware.py
```

完整演示 P1b：模拟电机台 + 噪声功率计 + 每点平均 5 次 + 安全限位 + SQLite 归档，然后
**从归档最优点续跑**、以及电机漂走后**一键回滚**到历史最优点。

---

## C2. 图 JSON（Dify 风格）直接运行

```bash
python run_graph.py                       # 跑 workflow_graph_example.json
python run_graph.py my_workflow.json      # 跑你自己的图
```

`workflow_graph_example.json` 是 `{nodes, edges}` 的节点+连线图（**未来托拉拽画布产出的就是这种 JSON**），
含一个 `balance_y2 ⇄ refine_y1` 的循环回边。脚本会先打印 mermaid 图（可粘到任意 mermaid 查看器预览），
再直接执行——**没有编译步骤，JSON 即运行**。自定义算法只需实现 `ask/tell` 并 `register_algorithm(...)` 即成为可用节点。

## C3. 托拉拽画布 + 后端 API（Dify 式）

```bash
python -m optplat.api          # 起后端服务（默认 http://127.0.0.1:8000）
```

浏览器打开 **http://127.0.0.1:8000/** 就是画布：

- 左侧「算法节点」点一下加节点（**中文名**，列表由后端 `/catalog` 动态生成，含你注册的自定义算法）
- 拖节点标题移动；点节点右侧圆点 ＋，再点另一个节点 → **连线**（连线时有跟随鼠标的橡皮筋预览，`Esc` 取消）
- 点节点 → 右侧编辑变量/目标/**目标模式(最大/最小/逼近目标值/扫描)**/参数/keep/stop.target
- 点**连线** → 右侧设**条件**（做分支或循环回边）或**删除连线**；也可选中后按 `Delete`、或在连线上点右键删除
- 顶部填全局 `until` 早停；用**字号**滑块放大画布字体；左下选评估方式（纯函数/模拟硬件+噪声/平均/安全）
- 点 **▶ 运行** → 右侧出指标 + **目标函数轨迹图**（可切「全部 / 间隔 N 点」看收敛）+ 编排轨迹
- **载入示例** 一键放好"找光→精调→均衡"三节点；右上 **帮助 ?** 有完整操作说明，关键处有 ⓘ 悬浮提示

画布**产出的就是 `{nodes, edges}` JSON**，POST 给 `/run/graph` 执行——和 `run_graph.py` 跑的是同一套。
画布是纯 vanilla JS，无 CDN、可离线。

后端接口：`GET /catalog`、`GET /vocs`、`POST /run/graph`、`POST /run/pipeline`（`/docs` 有自动 API 文档）。

## D. 跑测试（确认一切正常）

```bash
python -m pytest -q       # 11 个测试：8 种算法 + 两条流水线 + 硬件/安全/归档
```

---

## 接你自己的问题 / 硬件

- **换优化函数**：把 `optplat/demo.py` 里的 `optical_bench(x)` 换成你的 `f(x)`，改 `demo_vocs()` 的变量/目标数目即可。
- **接真硬件**：把 `SimulatedStage/SimulatedMeter` 换成用 **PyVISA/PyMeasure（MIT）** 封装的
  `move(axis, value)` / `read() -> dict` 薄类，其余代码不动（见 `README.md` 的 P1b 示例）。
- **写自己的流程**：编辑 YAML（`find_light_example.yaml` / `pipeline_example.yaml`）或直接传 pipeline dict。

用完把觉得缺的、别扭的地方告诉我，我来补。
