# 用 Agent 直接驱动优化平台（L2 · MCP 服务器）

这份文档教你**怎么把这个耦合优化平台接到一个 Agent（大模型）上**，让 Agent 能直接调用平台能力：
新增算法节点、加载以前的方案、跑仿真、对比不同策略、自动调优等。

一句话原理：**平台是 MCP 服务器（server），Agent 是 MCP 客户端（client）。**
Agent 通过一组「工具（tool）」调用平台；每次运行仍然走统一执行引擎和**安全限位**，
Agent 绕不过、也关不掉。

> MCP = Model Context Protocol，一个让大模型调用外部工具的开放标准。用的是官方 Python SDK（`mcp`，MIT 许可，permissive，闭源可用）。

---

## 0. 先装依赖 + 自检

```bash
pip install -r requirements.txt          # 已包含 mcp>=1.2
python -m pytest -q                       # 应 62 项全过（含 MCP 测试）
```

---

## 1. 启动服务器

三选一，按场景：

```bash
# ★ A) 统一服务（推荐，尤其要「Agent 改动→画布自动刷新」时）
python -m optplat.api
#   一个进程同时提供：画布(/)  +  REST  +  MCP(/mcp, streamable-HTTP)  +  实时工作区(/workspace)
#   → http://127.0.0.1:8000/mcp 给 Agent，http://127.0.0.1:8000/ 给用户看画布

# B) 纯 stdio —— Agent 和平台同机、无需画布联动（Claude Desktop / Cursor）
python run_mcp.py                 # 等价 python -m optplat.mcp_server

# C) 纯 MCP over HTTP（不带画布/REST）
python run_mcp.py --http          # http://127.0.0.1:8765/mcp
```

- **A 统一服务**：Hermes 连 `/mcp`，用户浏览器开 `/`，两者共享同一份**实时工作区**——Agent
  推流程/结果，画布自动刷新（见 §8）。**Hermes 场景用这个。**
- **B stdio**：客户端负责拉起进程，最省事，但没有画布联动。
- **C**：只要 MCP、多 Agent 连同一地址、不需要画布时用。

---

## 2. 平台暴露的工具（Agent 能做的事）

| 工具 | 作用 | 对应你的需求 |
|---|---|---|
| `list_algorithms` | 列出所有算法节点（含中文名/参数） | 让 Agent 知道有哪些算子可用 |
| `list_benches` | 列出仿真场景（单峰/多峰/…） | 选在哪个响应曲面上跑 |
| `get_default_vocs` | 变量范围 + 目标方向 + 通道成本 | 了解问题声明 |
| `new_workflow` | 新建空流程（start/end 骨架） | 搭流程的起点 |
| **`add_algorithm_node`** | **往流程里加一个算法节点** | **「新增某个算法节点」** |
| `list_solutions` | 列出所有方案（内置示例 + 已保存） | 看有哪些历史方案 |
| **`load_solution`** | **加载某个方案，取回可运行的流程** | **「加载某个以前的解决方案」** |
| `save_solution` | 把当前流程存成方案（可复用） | 让 Agent 沉淀方案 |
| `delete_solution` | 删除已保存方案 | 管理方案库 |
| **`run_workflow`** | **在仿真台上运行流程，返回结果** | **「进行仿真」** |
| **`compare_strategies`** | **同场景跑多种策略并对比、给推荐** | **「对比不同策略的结果」** |
| `autotune` | 平台自动搜索最优算法方案（质量/时长/稳定打分排名） | 让 Agent 一键找最优方案 |
| **`push_to_canvas`** | **把流程推到实时会话，画布自动刷新显示** | **「Agent 改了内部，界面自动更新」** |
| `get_canvas` | 读回会话当前状态（含用户在画布手改后的流程） | Agent ↔ 人双向协作 |
| `explain_result` | 把结果翻成中文小结 | 便于 Agent 转述 |

> 共 15 个工具。`run_workflow` / `autotune` 也接受 `session` 参数——带上就把结果一并写进实时会话，
> 画布对应面板自动刷新。

**典型一轮对话（Agent 内部调用顺序）：**

```
用户：在多峰场景下，比较「贝叶斯全局」和「网格找光+单纯形」哪种更好。
Agent →
  list_benches()                                     # 确认场景名 multi_peak
  load_solution("multi_peak_bayes")                  # 取一个现成的贝叶斯方案
  new_workflow("y1>=0.95 and y2>=0.9")               # 手搭对照方案
    add_algorithm_node(g, "grid_scan",  ["x1","x2"], "y1", stop_target="y1>0.2")
    add_algorithm_node(g, "nelder_mead",["x1","x2"], "y1")
    add_algorithm_node(g, "gaussian_fit",["x3"],     "y2", keep="y1>0.8")
  compare_strategies([
      {"name":"贝叶斯全局", "solution":"multi_peak_bayes"},
      {"name":"网格+单纯形","graph": g},
  ], bench="multi_peak", objective="y1")
Agent ←  ranking + recommended，用自然语言汇报给用户
```

---

## 3. 怎么配置（按你的 Agent 客户端选一个）

### 3.1 Claude Desktop（stdio）

编辑配置文件：
- macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows：`%APPDATA%\Claude\claude_desktop_config.json`

```jsonc
{
  "mcpServers": {
    "optplat": {
      "command": "python",
      "args": ["-m", "optplat.mcp_server"],
      "cwd": "/绝对路径/optimization-platform"
    }
  }
}
```

> 建议用虚拟环境里的 python 绝对路径，例如 `"command": "/绝对路径/.venv/bin/python"`，
> 免得系统 python 找不到依赖。改完**重启 Claude Desktop**，工具栏里会出现 optplat 的工具。

### 3.2 Cursor（stdio）

`~/.cursor/mcp.json`（或项目内 `.cursor/mcp.json`）：

```jsonc
{
  "mcpServers": {
    "optplat": {
      "command": "python",
      "args": ["-m", "optplat.mcp_server"],
      "cwd": "/绝对路径/optimization-platform"
    }
  }
}
```

### 3.3 公司内部 GLM5.1 Agent（HTTP，推荐给服务化场景）

1. 平台侧起 HTTP 服务：
   ```bash
   python run_mcp.py --http        # http://127.0.0.1:8765/mcp
   ```
   （要对内网开放就改 `optplat/mcp_server.py::main` 里的 host 为 `0.0.0.0`，并自行加访问控制。）

2. GLM5.1 侧作为 **MCP 客户端**连这个地址。任何兼容 MCP 的客户端库都行；
   下面给一段**通用 Python 客户端**示例（用官方 `mcp` SDK 连 streamable-HTTP）：

   ```python
   import asyncio, json
   from mcp import ClientSession
   from mcp.client.streamable_http import streamablehttp_client

   async def main():
       async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (r, w, _):
           async with ClientSession(r, w) as s:
               await s.initialize()
               tools = await s.list_tools()                 # 拿到工具清单 → 喂给 GLM5.1 的 tool schema
               # 让 GLM5.1 决定调用哪个工具后：
               res = await s.call_tool("run_workflow",
                                       {"graph": my_graph, "bench": "single_peak"})
               print(json.loads(res.content[0].text))       # 结果 JSON

   asyncio.run(main())
   ```

   **对接 GLM5.1 的套路**：`list_tools()` 拿到的每个工具都有 `name` / `description` /
   `inputSchema`（JSON Schema）——把它们转成 GLM5.1 的 function-calling 工具定义；
   模型产出 `{tool, arguments}` 后，你用 `call_tool(tool, arguments)` 执行，再把返回的
   JSON 塞回对话。这样 GLM5.1 就能「自己决定调用平台哪个能力」。

### 3.4 任意 MCP 客户端（自测/自研）

工具返回值约定（用官方 `mcp` SDK 手动解析时）：
- **返回单个对象的工具**（`new_workflow` / `add_algorithm_node` / `run_workflow` /
  `compare_strategies` / `autotune` / `load_solution` …）：结果 JSON 在
  `result.content[0].text`，`json.loads` 即可。
- **返回列表的工具**（`list_algorithms` / `list_benches` / `list_solutions`）：每个
  列表元素是**一个独立 content 块**，最省事是直接读 `result.structuredContent["result"]`
  拿到整个列表。

> 实际接大模型时你一般不用手动拆——function-calling 框架会把 tool 结果整体回灌给模型；
> 上面只是自研/自测客户端时的解析口径。仓库里 `tests/test_mcp.py` 的
> `test_stdio_client_server_roundtrip` 是一个最小可跑的客户端例子。

---

## 4. 各工具的参数速查

- `add_algorithm_node(graph, algorithm, variables, objective, params?, keep?, stop_target?)`
  - `algorithm`：算法英文 ID（`list_algorithms` 的 `name`，如 `grid_scan`/`nelder_mead`/`bayesian`）
  - `variables`：这一步优化哪些自变量，如 `["x1","x2"]`
  - `objective`：优化哪个因变量，如 `"y1"`
  - `params`：算法参数覆盖，如 `{"n_per_axis":9}`、`{"n_calls":60}`
  - `keep`：软约束（优化本步时保持成立），如 `"y1>0.8"`
  - `stop_target`：本步提前停止条件，如 `"y1>0.2"`（找光扫到阈值即停）
- `run_workflow(graph, bench?, noise?, averages?, safety?, eval_budget?, start_point?)`
  - `noise>0` 或 `safety=True` 走**带噪声/安全限位**的硬件模拟；返回 `objectives/state/n_evals/sim_seconds/fits/events`
- `compare_strategies(strategies, bench?, noise?, objective?)`
  - `strategies`：`[{"name","graph"} 或 {"name","solution":"方案名"}, ...]`
  - 返回每条的指标 + 按 `objective` 排序的 `ranking` 和 `recommended`
- `autotune(bench?, target?, quality_weight?, time_weight?, stability_weight?, noise_levels?, n_trials?, max_candidates?, top_k?)`
  - 「要快」→ 调高 `time_weight`；「要稳」→ 调高 `stability_weight`
  - 返回 top_k 候选（含指标、`utility`、是否帕累托最优、**可直接运行的 `graph`**）

---

## 5. 方案（解决方案）存在哪

- **内置示例**：每个仿真场景一条量身流程（`single_peak_default`/`multi_peak_bayes`/…），永远可用。
- **已保存方案**：`save_solution` 写到 `solutions/` 目录（可用环境变量
  `OPTPLAT_SOLUTIONS_DIR` 改位置）。JSON 文件，和画布『保存/方案库』互通。
- 该目录已在 `.gitignore` 里（属用户数据，不进源码）。

---

## 5b. 实时会话：Agent 改动 → 画布自动刷新

用统一服务（`python -m optplat.api`）时，Agent 和画布共享一份**实时工作区**，按**会话 ID**隔离。

**用户侧**：浏览器开 `http://<平台>/`，在左栏『实时会话 · Agent 联动』填**会话 ID**（如 `default`）
和 token（若平台设了），点『🔗 连接』。此后画布通过 SSE 监听该会话。

**Agent 侧**：搭好/改好流程后，带同一个会话调用即可让画布自动刷新：
```
push_to_canvas(graph, session="default", bench="multi_peak", note="已把 x3 拟合换成高斯")
run_workflow(graph, bench="multi_peak", session="default")     # 结果也回写、显示
autotune(bench="multi_peak", target="...", session="default")  # 排名结果显示在『自动调优』面板
```
**双向**：用户在画布拖改后点『↥ 同步给 Agent』，Agent 用 `get_canvas("default")` 读回最新流程再继续。

底层：`GET /workspace/{sid}`（拉快照）、`GET /workspace/{sid}/stream`（SSE，变化即推）、
`POST /workspace/{sid}`（画布回写）。每次写 `revision` 自增，画布据此判断重绘。

---

## 5c. 部署 + 鉴权（对接 Hermes 等托管 Agent）

托管 Agent（如 Hermes）连不到 `localhost`——把平台部署到 Agent 能访问的**内网地址**，并开 token：

```bash
OPTPLAT_TOKEN=<强随机串> python -m optplat.api      # 建议前置反代做 TLS，对外暴露 https://<host>/mcp
```
- `OPTPLAT_TOKEN` 一设，`/mcp` 与 `/workspace/*` 就要求 **Bearer token**（`get /health`、画布静态页不拦）。
- MCP 客户端在 `Authorization: Bearer <token>` 头里带；画布的 SSE 用 `?token=` 查询参数（浏览器
  EventSource 不能设头，已支持）。
- 不设 `OPTPLAT_TOKEN` = 开放，仅建议本机开发用。

---

## 5d. Hermes Agent：用 skill 一键接入（skill 捆绑连接）

仓库自带一个 skill 包 `skills/optplat/`，上传到 Hermes 对话即可让它自动连平台并会用：

```
skills/optplat/
  SKILL.md               # 教 Hermes 平台概念 + 主循环 + NL→IR 起草 + 结果呈现 + 纪律
  connect.json           # MCP 连接descriptor：把 url 换成你的内网地址、token 换成 OPTPLAT_TOKEN
  reference/ir_schema.md # graph IR 结构 + 15 个工具速查 + 算法一览
  reference/recipes.md   # 按场景的起草配方（单峰/多峰/相关谷/多峰…）
```

**步骤**：① 平台按 §5c 起服务并拿到 `https://<host>/mcp` + token；② 编辑 `connect.json` 填上
url 和 token；③ 把 `skills/optplat/` 打包上传到 Hermes 对话。Hermes 读 `connect.json` 连上
`optplat` MCP 服务器，读 `SKILL.md` 学会用法，之后你直接用自然语言下指令，Hermes 就会搭流程、
`push_to_canvas` 让你在平台界面实时看到、跑仿真/对比/自动调优，并把结果解析给你。

> 若你的 Hermes 版本要求在**宿主/管理端**配 MCP 连接（而非 skill 捆绑），就把 `connect.json` 的
> url/token 配到 Hermes 的 MCP 设置里，`SKILL.md` + `reference/` 仍作纯用法说明上传即可。

---

## 6. 安全与合规

- **护栏不可绕过**：Agent 只能提交 IR（流程 JSON）；运行时循环强制上限、评估预算熔断、
  条件用白名单表达式、**硬件安全限位在 Evaluator 层夹回越界运动**——任何 Agent 指令都不能命令执行器越界。
- **无外部 LLM 依赖**：MCP 服务器本身不调用任何大模型；接的是**你自己的 Agent**（内部 GLM5.1）。
- **依赖全 permissive**：新增的 `mcp` 是 MIT，已登记 `THIRD_PARTY_LICENSES.md`。

---

## 7. 下一步（可选增强）

- **NL→IR 已可用（Hermes 原生）**：Hermes 读 skill 后自己把自然语言拆成工具调用起草流程，平台做
  schema 校验。无需平台内再塞 LLM。
- **画布 Copilot（待做，可选）**：给网页端加一个自然语言输入框，平台内部用 GLM5.1 出一份校验过的 IR
  （`draft_workflow`），供不经 Hermes 的用户直接在界面里打字生成流程。
- **真实硬件**：把仿真 bench 换成 PyVISA/PyMeasure 封装的真机后，这些 MCP 工具**一行不用改**即可驱动真实台子。
- **多用户/持久化**：实时工作区目前是进程内内存版（按 session 隔离）；要多租户/断电续存再上持久化 + 队列。
