"""Tests for the L2 MCP server (optplat.mcp_server) and the solution store.

The FastMCP-decorated tools remain plain callables, so we exercise them
directly (fast, no transport). One async test does a real stdio client↔server
round-trip to prove the wire integration works end to end.
"""
import asyncio
import json

import pytest

from optplat import mcp_server as m
from optplat import solutions as sol
from optplat.workspace import WorkspaceStore


# ---------------- solution store ----------------
def test_builtin_solutions_present():
    names = {s["name"] for s in sol.list_solutions() if s["source"] == "builtin"}
    assert {"single_peak_default", "multi_peak_bayes", "ackley_bayes"} <= names


def test_get_builtin_solution_graph_runs():
    rec = sol.get_solution("single_peak_default")
    assert rec["bench"] == "single_peak"
    algos = [n["data"]["algorithm"] for n in rec["graph"]["nodes"] if n["type"] == "algorithm"]
    assert algos == ["grid_scan", "nelder_mead", "formula"]


def test_save_load_delete_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPTPLAT_SOLUTIONS_DIR", str(tmp_path))
    g = m.new_workflow("y1>=0.9")
    g = m.add_algorithm_node(g, "coordinate_descent", ["x1", "x2"], "y1")
    m.save_solution("我的方案", g, bench="single_peak", description="test")
    names = {s["name"] for s in sol.list_solutions() if s["source"] == "saved"}
    assert "我的方案" in names
    back = sol.get_solution("我的方案")
    assert back["graph"]["nodes"][1]["data"]["algorithm"] == "coordinate_descent"
    assert sol.delete_solution("我的方案") is True


def test_cannot_delete_builtin():
    with pytest.raises(ValueError):
        sol.delete_solution("single_peak_default")


# ---------------- graph editing tools ----------------
def test_new_workflow_skeleton():
    g = m.new_workflow("y1>=0.98")
    types = [n.get("type") for n in g["nodes"]]
    assert types == ["start", "end"]
    assert g["until"] == "y1>=0.98"
    assert {"source": "start", "target": "end"} in g["edges"]


def test_add_algorithm_node_inserts_before_end_and_rewires():
    g = m.new_workflow()
    g = m.add_algorithm_node(g, "grid_scan", ["x1", "x2"], "y1", stop_target="y1>0.2")
    g = m.add_algorithm_node(g, "nelder_mead", ["x1", "x2"], "y1")
    order = [n["id"] for n in g["nodes"]]
    assert order[0] == "start" and order[-1] == "end"
    algos = [n["data"]["algorithm"] for n in g["nodes"] if n.get("type") == "algorithm"]
    assert algos == ["grid_scan", "nelder_mead"]
    # linear chain start -> n1 -> n2 -> end, no dangling edge into end from start
    succ = {(e["source"], e["target"]) for e in g["edges"]}
    assert ("start", "end") not in succ
    assert ("n2", "end") in succ
    # stop_target lands in node data
    n1 = next(n for n in g["nodes"] if n["id"] == "n1")
    assert n1["data"]["stop"]["target"] == "y1>0.2"


def test_add_unknown_algorithm_rejected():
    with pytest.raises(ValueError):
        m.add_algorithm_node(m.new_workflow(), "no_such_algo", ["x1"], "y1")


# ---------------- run / compare / autotune ----------------
def test_run_workflow_reaches_peak():
    g = m.new_workflow("y1>=0.98 and y2>=0.95")
    g = m.add_algorithm_node(g, "grid_scan", ["x1", "x2"], "y1", stop_target="y1>0.2")
    g = m.add_algorithm_node(g, "nelder_mead", ["x1", "x2"], "y1")
    g = m.add_algorithm_node(g, "formula", ["x3"], "y2", keep="y1>0.8")
    res = m.run_workflow(g, bench="single_peak")
    assert res["objectives"]["y1"] > 0.95
    assert res["n_evals"] > 0
    assert res["sim_seconds"] > 0


def test_compare_strategies_ranks_and_recommends():
    g = m.new_workflow("y1>=0.9")
    g = m.add_algorithm_node(g, "coordinate_descent", ["x1", "x2"], "y1")
    out = m.compare_strategies(
        [{"name": "preset", "solution": "single_peak_default"},
         {"name": "coord", "graph": g}],
        bench="single_peak", objective="y1")
    assert set(out["ranking"]) == {"preset", "coord"}
    assert out["recommended"] in {"preset", "coord"}
    assert all(r["ok"] for r in out["results"])


def test_compare_strategy_missing_graph_errors():
    with pytest.raises(ValueError):
        m.compare_strategies([{"name": "bad"}], bench="single_peak")


def test_autotune_returns_ranked_candidates():
    out = m.autotune(bench="single_peak", max_candidates=6, n_trials=1,
                     noise_levels=[0.0], top_k=3)
    assert out["n_candidates"] > 0
    assert 1 <= len(out["ranked"]) <= 3
    top = out["ranked"][0]
    assert "graph" in top and "utility" in top
    # the returned graph is directly runnable
    res = m.run_workflow(top["graph"], bench="single_peak")
    assert "y1" in res["objectives"]


def test_explain_result_is_chinese_summary():
    res = m.run_workflow(sol.get_solution("single_peak_default")["graph"], bench="single_peak")
    text = m.explain_result(res)
    assert "最终目标" in text and "评估" in text


# ---------------- live workspace ----------------
def test_workspace_store_revision_bumps_and_merges():
    ws = WorkspaceStore()
    assert ws.revision("s") == 0
    snap = ws.update("s", graph={"nodes": []}, bench="multi_peak")
    assert snap["revision"] == 1 and snap["bench"] == "multi_peak"
    snap = ws.update("s", note="hi")            # partial update keeps prior fields
    assert snap["revision"] == 2 and snap["bench"] == "multi_peak" and snap["note"] == "hi"
    assert ws.snapshot("s")["graph"] == {"nodes": []}


def test_push_and_get_canvas_roundtrip():
    sid = "test_sess_" + str(id(object()))
    g = m.new_workflow("y1>=0.9")
    g = m.add_algorithm_node(g, "grid_scan", ["x1", "x2"], "y1", stop_target="y1>0.2")
    out = m.push_to_canvas(g, session=sid, bench="multi_peak", note="草稿")
    assert out["pushed"] and out["revision"] >= 1
    back = m.get_canvas(sid)
    assert back["bench"] == "multi_peak" and back["note"] == "草稿"
    algos = [n["data"]["algorithm"] for n in back["graph"]["nodes"] if n.get("type") == "algorithm"]
    assert algos == ["grid_scan"]


def test_run_and_autotune_write_to_session():
    sid = "test_run_" + str(id(object()))
    g = sol.get_solution("single_peak_default")["graph"]
    m.run_workflow(g, bench="single_peak", session=sid)
    snap = m.get_canvas(sid)
    assert snap["result"] is not None and "y1" in snap["result"]["objectives"]
    m.autotune(bench="single_peak", max_candidates=4, n_trials=1,
               noise_levels=[0.0], top_k=2, session=sid)
    snap = m.get_canvas(sid)
    assert snap["autotune"] is not None and snap["autotune"]["ranked"]


# ---------------- tool registry ----------------
def test_all_tools_registered():
    tools = asyncio.run(m.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"list_algorithms", "add_algorithm_node", "load_solution",
            "run_workflow", "compare_strategies", "autotune",
            "push_to_canvas", "get_canvas"} <= names


# ---------------- real stdio wire round-trip ----------------
def test_stdio_client_server_roundtrip():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    def J(res):
        return json.loads(res.content[0].text)

    async def go():
        params = StdioServerParameters(command="python", args=["-m", "optplat.mcp_server"])
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                assert len(tools.tools) >= 13
                g = J(await s.call_tool("new_workflow", {"until": "y1>=0.98"}))
                g = J(await s.call_tool("add_algorithm_node",
                                        {"graph": g, "algorithm": "grid_scan",
                                         "variables": ["x1", "x2"], "objective": "y1",
                                         "stop_target": "y1>0.2"}))
                g = J(await s.call_tool("add_algorithm_node",
                                        {"graph": g, "algorithm": "nelder_mead",
                                         "variables": ["x1", "x2"], "objective": "y1"}))
                run = J(await s.call_tool("run_workflow", {"graph": g, "bench": "single_peak"}))
                assert run["objectives"]["y1"] > 0.9

    asyncio.run(go())
