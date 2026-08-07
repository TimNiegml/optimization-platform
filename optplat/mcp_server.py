"""L2 · MCP server — expose the optimization platform to any MCP agent.

Runs the platform as an MCP *server*; an agent (internal GLM5.1, Claude Desktop,
Cursor, …) is the *client*. The agent drives the platform through a handful of
typed tools — build a workflow, add an algorithm node, load a saved 方案, run a
simulation, compare strategies, auto-tune — while every run still goes through
the same StageEngine and safety guardrails, which the agent can neither see
around nor switch off.

Run it:
    python -m optplat.mcp_server            # stdio transport (Claude Desktop / Cursor)
    python -m optplat.mcp_server --http     # streamable-HTTP transport on :8765

Official MCP Python SDK (`mcp`, MIT) — permissive, closed-source friendly.
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import solutions as sol
from .audit import audit_solution as _audit_solution
from .audit import compare_solutions as _compare_solutions
from .autotune import TuneSpec, run_autotune
from .benchsuite import get_suite
from .demo import BENCHES, demo_vocs
from .registry import REGISTRY, algorithm_catalog
from .runner import run_workflow as _run_workflow
from .sensitivity import SensitivitySpec, run_sensitivity
from .trace_digest import digest, digest_many
from .workspace import WORKSPACE

mcp = FastMCP(
    "optplat",
    instructions=(
        "光器件耦合/标定优化平台。用这些工具：先 list_algorithms / list_benches / list_solutions "
        "了解可用能力；用 new_workflow + add_algorithm_node 搭一条『找光→精调→拟合』流程，或用 "
        "load_solution 加载以前的方案；用 run_workflow 在仿真台上跑；用 compare_strategies 对比多种"
        "策略；用 autotune 让平台自动搜索最优算法方案。搭好或改好流程后用 push_to_canvas(带用户给的 "
        "session) 把它推给正在看的画布——画布会自动刷新；用 get_canvas 读回用户手改后的流程。"
        "run_workflow / autotune 也可带 session，结果会自动显示在画布。所有运行都经过统一引擎与安全"
        "限位，不会命令执行器越界。变量默认 x1,x2,x3；目标默认 y1(耦合功率,最大化)、y2(WDL均衡)。"
    ),
)


# ============================ discovery ============================
@mcp.tool()
def list_algorithms() -> list[dict]:
    """列出所有可用算法节点（可拖成流程的算子）。

    返回每个算法的 name(英文ID)/label(中文名)/category(find-light|local|fit|
    analytic|bayesian)/single_var(是否单变量)/params(参数schema，含默认值与范围)/
    desc(说明)。搭流程时用 name 作为 add_algorithm_node 的 algorithm 参数。
    """
    return algorithm_catalog()


@mcp.tool()
def list_benches() -> list[dict]:
    """列出可选的仿真评估场景（bench）。

    每个场景共享同一变量空间 (x1,x2,x3 → y1,y2)，代表不同的响应曲面（单峰/多峰/
    偏斜/Rosenbrock/Rastrigin/Ackley）。run_workflow / autotune 的 bench 参数用其 name。
    """
    return [{"name": n, "label": b["label"], "desc": b["desc"], "smooth": b.get("smooth", False)}
            for n, b in BENCHES.items()]


@mcp.tool()
def get_default_vocs() -> dict:
    """返回默认问题声明（VOCS）：自变量的范围、因变量的优化方向与通道成本。"""
    v = demo_vocs()
    return {
        "variables": {n: {"low": var.low, "high": var.high} for n, var in v.variables.items()},
        "objectives": {n: {"mode": o.mode.value, "cost_seconds": o.cost,
                           "device": o.device, "param": o.param}
                       for n, o in v.objectives.items()},
    }


# ============================ build a workflow ============================
@mcp.tool()
def new_workflow(until: Optional[str] = None) -> dict:
    """新建一条空流程（含 start/end 骨架）。

    返回一个 graph JSON {nodes, edges, until?}。之后用 add_algorithm_node 往里加算子。
    until 是全局早停表达式（如 "y1>=0.98 and y2>=0.95"），满足即整体停止。
    """
    graph: dict = {"nodes": [{"id": "start", "type": "start"},
                             {"id": "end", "type": "end"}],
                   "edges": [{"source": "start", "target": "end"}]}
    if until:
        graph["until"] = until
    return graph


@mcp.tool()
def add_algorithm_node(graph: dict, algorithm: str, variables: list[str],
                       objective: str, params: Optional[dict] = None,
                       keep: Optional[str] = None, stop_target: Optional[str] = None,
                       node_id: Optional[str] = None) -> dict:
    """向流程新增一个算法节点（插在 end 之前，串在当前流程末尾）。

    这是 agent『新增某个算法节点』的入口。
      algorithm    算法 name（见 list_algorithms，如 grid_scan / nelder_mead / bayesian）
      variables    这一步优化哪些自变量，如 ["x1","x2"]
      objective    这一步优化哪个因变量，如 "y1"
      params       该算法的参数覆盖，如 {"n_per_axis":9} 或 {"n_calls":60}（可选）
      keep         软约束表达式，优化本步时保持它成立，如 "y1>0.8"（可选）
      stop_target  本步提前停止条件，如 "y1>0.2"（找光扫到阈值即停）（可选）
    返回更新后的 graph JSON（可继续 add 或直接 run_workflow）。
    """
    if algorithm not in REGISTRY:
        raise ValueError(f"未知算法 '{algorithm}'，可用: {sorted(REGISTRY)}")
    g = copy.deepcopy(graph) if graph else new_workflow()
    g.setdefault("nodes", [])
    g.setdefault("edges", [])

    # ensure start & end exist
    ids = {n["id"] for n in g["nodes"]}
    if not any(n.get("type") == "start" for n in g["nodes"]):
        g["nodes"].insert(0, {"id": "start", "type": "start"})
    end = next((n["id"] for n in g["nodes"] if n.get("type") == "end"), None)
    if end is None:
        end = "end"
        g["nodes"].append({"id": end, "type": "end"})

    # unique node id
    nid = node_id or f"n{sum(1 for n in g['nodes'] if n.get('type') == 'algorithm') + 1}"
    while nid in ids:
        nid += "_"

    data: dict[str, Any] = {"algorithm": algorithm, "variables": list(variables),
                            "objective": objective, "stage": REGISTRY[algorithm].label or algorithm}
    if params:
        data.update(params)
    if keep:
        data["keep"] = keep
    if stop_target:
        data["stop"] = {"target": stop_target}
    g["nodes"].insert(len(g["nodes"]) - 1 if g["nodes"][-1]["id"] == end else len(g["nodes"]),
                      {"id": nid, "type": "algorithm", "data": data})

    # rewire: whatever fed `end` (default edge) now feeds the new node → end
    into_end = [e for e in g["edges"] if e.get("target") == end and not e.get("condition")]
    if into_end:
        for e in into_end:
            e["target"] = nid
    g["edges"].append({"source": nid, "target": end})
    return g


# ============================ solutions (方案) ============================
@mcp.tool()
def list_solutions() -> list[dict]:
    """列出所有可用方案：内置示例（每个场景一条量身流程）+ 已保存的方案。

    每条含 name/source(builtin|saved)/bench/description/steps(算子序列)/until。
    用 load_solution(name) 取回完整 graph。
    """
    return sol.list_solutions()


@mcp.tool()
def load_solution(name: str) -> dict:
    """加载某个以前的方案，返回其完整记录（含可直接运行的 graph）。

    这是 agent『加载某个以前的解决方案』的入口。name 来自 list_solutions。
    返回 {name, source, bench, description, graph}；把 graph 交给 run_workflow 即可跑。
    """
    return sol.get_solution(name)


@mcp.tool()
def save_solution(name: str, graph: dict, bench: str = "single_peak",
                  description: str = "") -> dict:
    """把当前 graph 存成一个可复用的方案（同名覆盖），之后可用 load_solution 取回。"""
    rec = sol.save_solution(name, graph, bench=bench, description=description)
    return {"saved": rec["name"], "path_dir": sol.solutions_dir(),
            "bench": rec["bench"], "description": rec["description"]}


@mcp.tool()
def delete_solution(name: str) -> dict:
    """删除一个已保存的方案（内置示例不可删）。"""
    removed = sol.delete_solution(name)
    return {"deleted": removed, "name": name}


# ============================ run & compare ============================
@mcp.tool()
def run_workflow(graph: dict, bench: str = "single_peak", noise: float = 0.0,
                 averages: int = 1, safety: bool = False,
                 eval_budget: int = 5000, start_point: Optional[dict] = None,
                 session: Optional[str] = None) -> dict:
    """在仿真台上运行一条流程（graph），返回结果摘要。

    这是 agent『进行仿真』的入口。
      graph        由 new_workflow/add_algorithm_node 搭出，或 load_solution 取回
      bench        仿真场景 name（见 list_benches）
      noise        测量噪声 σ（>0 走带噪声/安全的硬件模拟）
      averages     多次平均以抑噪
      safety       是否启用逐变量安全限位（夹回越界运动）
      session      给了就把结果 + 流程回写到该实时会话，画布自动刷新显示（见 push_to_canvas）
    返回 objectives(最终 y)、state(最终 x)、n_evals、sim_seconds(测量耗时)、
    fits(拟合公式)、events(逐步日志)。
    """
    res = _run_workflow(graph, bench=bench, noise=noise, averages=averages,
                        safety=safety, eval_budget=eval_budget, start_point=start_point)
    if session:
        WORKSPACE.update(session, graph=graph, bench=bench, result=res)
    return res


@mcp.tool()
def compare_strategies(strategies: list[dict], bench: str = "single_peak",
                       noise: float = 0.0, averages: int = 1,
                       objective: str = "y1", eval_budget: int = 5000) -> dict:
    """在同一场景下运行多种策略并对比结果（质量/耗时/评估次数），给出推荐。

    这是 agent『对比不同策略的结果』的入口。
      strategies   策略列表，每项：
                     {"name": "...", "graph": {...}}         直接给流程，或
                     {"name": "...", "solution": "方案名"}    引用一个已存方案
      bench        统一的仿真场景
      objective    以哪个目标排名（默认 y1，越大越好）
    返回每条策略的 objectives/sim_seconds/n_evals，以及按 objective 排序的 ranking
    与 recommended（质量最高者）。
    """
    rows = []
    for i, st in enumerate(strategies):
        name = st.get("name") or f"strategy_{i + 1}"
        if st.get("solution"):
            graph = sol.get_solution(st["solution"])["graph"]
        elif st.get("graph"):
            graph = st["graph"]
        else:
            raise ValueError(f"策略 '{name}' 需要 'graph' 或 'solution' 字段")
        try:
            res = _run_workflow(graph, bench=bench, noise=noise, averages=averages,
                                eval_budget=eval_budget)
            rows.append({"name": name, "ok": True,
                         "objectives": res["objectives"],
                         "score": float(res["objectives"].get(objective, float("nan"))),
                         "sim_seconds": res["sim_seconds"], "n_evals": res["n_evals"],
                         "fits": res["fits"]})
        except Exception as e:
            rows.append({"name": name, "ok": False, "error": f"{type(e).__name__}: {e}",
                         "score": float("-inf")})

    ranked = sorted(rows, key=lambda r: (r.get("score") if r.get("score") == r.get("score")
                                         else float("-inf")), reverse=True)
    best = next((r for r in ranked if r.get("ok")), None)
    return {"bench": bench, "objective": objective, "results": rows,
            "ranking": [r["name"] for r in ranked],
            "recommended": best["name"] if best else None}


# ============================ live canvas sync ============================
@mcp.tool()
def push_to_canvas(graph: dict, session: str = "default",
                   bench: Optional[str] = None, note: str = "") -> dict:
    """把一条流程（graph）推到实时会话，正在看该会话的画布会**自动刷新**显示。

    这是 agent『改了内部、让界面自动刷新』的入口。起草/修改好流程后显式推一次即可。
      graph     要显示的流程（new_workflow/add_algorithm_node 搭出或 load_solution 取回）
      session   会话 ID（用户在画布『连接实时会话』里填的同一个），默认 "default"
      bench     可选，同时切换画布的仿真场景
      note      可选，一句给用户看的说明（如"已把 x3 拟合换成高斯"）
    返回该会话的最新 revision（画布据此判断是否需要重绘）。
    """
    snap = WORKSPACE.update(session, graph=graph, bench=bench, note=note or None)
    return {"session": session, "revision": snap["revision"],
            "updated_at": snap["updated_at"], "pushed": True}


@mcp.tool()
def get_canvas(session: str = "default") -> dict:
    """读回某个实时会话的当前状态（含用户在画布上手改后的最新 graph / 场景 / 上次结果）。

    用于 agent ↔ 人双向协作：用户在画布上拖改后，agent 用这个拿到最新流程再继续。
    返回里的 `messages` 是画布聊天记录（role=user 是用户在画布聊天框发来的指令、role=agent
    是你之前 push 的 note）；读到新的 user 消息就按它执行，然后用 push_to_canvas 回复。
    """
    snap = WORKSPACE.snapshot(session)
    return {"session": session, "graph": snap.get("graph"), "bench": snap.get("bench"),
            "result": snap.get("result"), "autotune": snap.get("autotune"),
            "note": snap.get("note", ""), "messages": snap.get("messages", []),
            "revision": snap.get("revision", 0), "updated_at": snap.get("updated_at")}


@mcp.tool()
def poll_messages(session: str = "default", mark_read: bool = True) -> dict:
    """拉取用户在平台画布聊天框发来、你还没读过的指令（你的收件箱）。

    为什么需要它：平台是 MCP 服务器、你是客户端，平台无法主动叫醒你——用户在画布上
    发的指令只会先存进会话，等你来取。**建议每次和用户交互一开始、以及长任务中途，
    都先调用一次 `poll_messages(session)`**：把收件箱里的用户新指令读出来优先处理，
    处理/回应完再用 `push_to_canvas(session=...)` 把结果推回画布。

    参数：
      session    用户在画布『实时会话』里填的会话 ID（默认 default），要和其它调用一致。
      mark_read  取出后标记为已读（默认 True），保证每条指令只被消费一次；传 False 只看不消费。
    返回：{session, messages:[{role:"user", text, ts}...], count}。count=0 表示没有新指令。
    """
    msgs = WORKSPACE.poll_user_messages(session, mark_read=mark_read)
    return {"session": session, "messages": msgs, "count": len(msgs)}


# ============================ auto-tune ============================
@mcp.tool()
def autotune(bench: str = "single_peak", target: Optional[str] = "y1>=0.95 and y2>=0.9",
             quality_weight: float = 1.0, time_weight: float = 0.5,
             stability_weight: float = 1.0, noise_levels: Optional[list[float]] = None,
             n_trials: int = 2, max_candidates: int = 12, top_k: int = 5,
             session: Optional[str] = None) -> dict:
    """让平台自动搜索最优算法方案（外层优化），按 质量/时长/稳定性 打分排名。

    在『粗调→精调→拟合』三相空间里枚举候选流程，多噪声/多seed评估，标量效用排序 +
    帕累托前沿。返回 top_k 候选（含指标、utility、是否帕累托最优、可直接运行的 graph）。
      target            达标定义（决定稳定性=达标率；None 时用变异系数）
      *_weight          质量/时长/稳定 的权重（要快→调高 time_weight，要稳→调高 stability_weight）
      session           给了就把排名结果回写到该实时会话，画布『自动调优』面板自动刷新
    采用某候选：把它的 graph 交给 run_workflow / push_to_canvas，或用 save_solution 存起来。
    """
    spec = TuneSpec(
        bench=bench, target=target,
        weights={"quality": quality_weight, "time": time_weight, "stability": stability_weight},
        noise_levels=noise_levels if noise_levels is not None else [0.0, 0.02],
        n_trials=n_trials, max_candidates=max_candidates,
    )
    out = run_autotune(spec)
    ranked = out["ranked"][:max(1, top_k)]
    slim = [{"id": r["id"], "label": r["label"],
             "quality": round(r["quality"], 4), "time": round(r["time"], 3),
             "stability": round(r["stability"], 3), "evals": round(r.get("evals", 0.0), 1),
             "utility": round(r["utility"], 4), "pareto": r.get("pareto", False),
             "graph": r["graph"]} for r in ranked]
    result = {"bench": bench, "n_candidates": out["n_candidates"],
              "weights": spec.weights, "ranked": slim,
              "best": slim[0]["label"] if slim else None}
    if session:
        WORKSPACE.update(session, bench=bench, autotune=result)
    return result


# ============================ 灵敏度采集 ============================
@mcp.tool()
def measure_sensitivity(variables: list[str], objectives: Optional[list[str]] = None,
                        step: float = 0.1, n_points: int = 5,
                        origins: Optional[list[dict]] = None, fit: str = "linear",
                        bench: str = "single_peak", noise: float = 0.0,
                        averages: int = 1, linear_tol: float = 0.05,
                        session: Optional[str] = None) -> dict:
    """灵敏度采集：逐轴扫描测出 ∂y/∂x 矩阵与线性范围（测量，不是优化）。

    以每个原点为中心，把每个自变量单独走 n_points 个点（其余轴钉住），拟合出原点处斜率，
    装配成矩阵。矩阵形状与 `damped_sensitivity` 节点的 `sensitivity` 入参一致——**测完可以
    直接拿去搭多进多出定值流程**。

      variables   要动的自变量，如 ["x1","x2"]
      objectives  要测的因变量；留空=全部
      step        步距（绝对值），n_points 每轴点数（含原点，建议奇数）
      origins     原点列表，如 [{"x1":2,"x2":0},{"x1":4,"x2":1}]；留空=用默认起点。
                  **给多个原点可以看灵敏度随工作点漂移多少**——漂移大说明非线性强、该分段建模。
      fit         linear | quadratic（二次时斜率取原点处导数，曲线弯时更准）
      linear_tol  线性范围判据：偏离原点切线 ≤ 该比例×y跨度就算线性
      session     给了就把结果推到该实时会话，画布『📐 灵敏度采集』面板自动刷新
    返回：matrix(∂y/∂x)、matrix_std(多原点时的漂移)、每对 x→y 的 R²/线性范围、summary 结论。
    """
    from .demo import bench_func
    from .evaluator import Evaluator
    from .hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage

    vocs = demo_vocs()
    func = bench_func(bench)
    if noise or averages > 1:
        stage = SimulatedStage(vocs.initial_point())
        ev = HardwareEvaluator(stage, SimulatedMeter(stage, func, noise=noise, seed=0),
                               averages=averages)
    else:
        ev = Evaluator(func)
    spec = SensitivitySpec(variables=list(variables), objectives=list(objectives or []),
                           step=step, n_points=n_points, origins=list(origins or []),
                           fit=fit, linear_tol=linear_tol)
    out = run_sensitivity(vocs, ev, spec)
    slim = {"bench": bench, "variables": out["variables"], "objectives": out["objectives"],
            "fit": out["fit"], "matrix": out["matrix"], "matrix_std": out["matrix_std"],
            "n_origins": out["n_origins"], "n_evals": out["n_evals"],
            "summary": out["summary"], "warnings": out["warnings"],
            "linearity": {o: {v: {"r2": round(r["fits"][o][v]["r2"], 4),
                                  "linear_range": r["fits"][o][v].get("linear_range")}
                              for v in out["variables"]}
                          for o in out["objectives"] for r in out["origins"][:1]}}
    if session:
        WORKSPACE.update(session, bench=bench, sensitivity=out)
    return slim


# ============================ explain ============================
@mcp.tool()
def explain_result(result: dict) -> str:
    """把一次 run_workflow 的结果翻成一段中文小结（便于 agent 转述给用户）。"""
    objs = result.get("objectives", {})
    parts = [f"在场景『{result.get('bench', '?')}』上运行完成。"]
    if objs:
        parts.append("最终目标：" + "、".join(f"{k}={v:.4g}" for k, v in objs.items()) + "。")
    st = result.get("state", {})
    if st:
        parts.append("最优操作点：" + "，".join(f"{k}={v:.3g}" for k, v in st.items()) + "。")
    parts.append(f"共评估 {result.get('n_evals', 0)} 次，"
                 f"模拟测量耗时约 {result.get('sim_seconds', 0)} 秒。")
    if result.get("fits"):
        parts.append("拟合：" + "；".join(f"{k}: {v}" for k, v in result["fits"].items()) + "。")
    return " ".join(parts)


# ============================ 审计 / 诊断（评判 agent 用）============================
@mcp.tool()
def diagnose_run(graph: dict, bench: str = "single_peak", noise: float = 0.0,
                 averages: int = 1, n_runs: int = 3, objective: str = "y1",
                 start_point: Optional[dict] = None,
                 eval_budget: int = 2000) -> dict:
    """跑一条流程若干次并把 trace 压成**可读的诊断报告**（含建议搜索区间）。

    这是你（Agent）『看 trace 改算法』的入口。返回：
      stage_attribution  每个阶段花了多少评估、贡献了多少增益（揪出"贵而无用"的阶段）
      issues             失效模式：振荡/停滞/卡边界/平坦区空扫/拟合被拒，每条都带触发证据
                         **只信 reliable=true 的**（多数运行都复现），单次出现的多半是噪声
      length_scales      从 trace 实测的每个自变量『峰宽』——决定步距的物理量
      suggested_search_space  由峰宽推出的**搜索区间**，直接可喂给 autotune 的 search_space

    用法：读 issues 判断病灶 → 核对 length_scales 是否合理 → 把
    suggested_search_space（可按你的判断收窄）传给 autotune(search_space=...)。
    注意：你负责判断"哪个旋钮错了、往哪个方向调"，**具体数值交给 autotune 搜**，不要自己拍数字。
    """
    n = max(1, min(int(n_runs), 8))
    results = []
    for i in range(n):
        results.append(_run_workflow(graph, bench=bench, noise=noise, averages=averages,
                                     eval_budget=eval_budget, start_point=start_point,
                                     keep_history=True, seed=i))
    v = demo_vocs()
    return digest_many(results, v, objective) if n > 1 else digest(results[0], v, objective)


@mcp.tool()
def list_benchmark_suites() -> dict:
    """列出题库：dev（可见，用于调优/诊断）与 frozen（冻结验收题库）。

    frozen 是**现实锚**：设计算法时不可针对它调参，只用于最终验收。返回的
    fingerprint 是题库内容哈希——审计报告里会带上，可复核题库未被改动过。
    """
    return {"suites": [get_suite("dev").summary(), get_suite("frozen").summary()],
            "rule": ("dev 可自由调优；frozen 只做验收。若发现有人针对 frozen 调参，"
                     "该结果作废——泛化间隙就是为检测这件事而存在。")}


@mcp.tool()
def audit_solution(graph: dict, label: str = "candidate") -> dict:
    """【评判用】在 dev + frozen 两套题库上审计一条方案，返回确定性证据。

    你（评判 Agent）基于本报告下结论，但**不能改动分数**——打分是确定性计算，
    这样循环才不会退化成"想办法说服裁判"。重点看：
      acceptance            PASS/FAIL（硬性检查：冻结达标率、运行报错）
      frozen.*              冻结题库上的真实表现（唯一有效的性能依据）
      generalization_gap    DEV 与 FROZEN 的差距。间隙大有两种成因，需要区分：
                            ①方案被针对可见题目调过；②冻结题库本身更难。
                            若只有该方案间隙大 → 更像①；若所有方案都大 → 更像②
      flags                 每条都带 detail（触发它的具体数字）与 meaning
      provenance            两套题库的 fingerprint，可复核未被篡改

    请在结论里明确写：通过/不通过、依据哪几个数字、以及若不通过该改什么。
    """
    return _audit_solution(graph, label=label)


@mcp.tool()
def compare_solutions(candidates: list[dict], top_k: int = 5) -> dict:
    """【评判用】审计并排名多条方案。candidates=[{label, graph}, ...]

    排名**只依据 FROZEN 表现**（达标率→质量→耗时），DEV 分数不参与排名，
    只用来计算泛化间隙。返回 ranking（含每条的 flags）与 best。
    """
    out = _compare_solutions(candidates)
    out["reports"] = out["reports"][: max(1, int(top_k))]
    return out


def main() -> None:
    import sys
    if "--http" in sys.argv:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = 8765
        mcp.run(transport="streamable-http")
    else:
        mcp.run()          # stdio (default)


if __name__ == "__main__":
    main()
