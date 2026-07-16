"""API tests via FastAPI TestClient (needs httpx). Skipped if httpx absent."""
import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient   # noqa: E402

from optplat.api import app                 # noqa: E402
from optplat.demo import TWO_PHASE_GRAPH     # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_catalog_lists_algorithms():
    algos = client.get("/catalog").json()["algorithms"]
    names = {a["name"] for a in algos}
    assert {"grid_scan", "nelder_mead", "formula", "bayesian"} <= names
    # each entry carries the param schema the canvas renders
    assert all("params" in a and "category" in a for a in algos)


def test_vocs():
    d = client.get("/vocs").json()
    assert set(d["variables"]) == {"x1", "x2", "x3"}


def test_run_graph():
    r = client.post("/run/graph", json={"graph": TWO_PHASE_GRAPH,
                                        "evaluator": {"mode": "function"}})
    assert r.status_code == 200
    d = r.json()
    assert d["objectives"]["y1"] > 0.98 and d["objectives"]["y2"] > 0.95


def test_run_graph_hardware_sim():
    r = client.post("/run/graph", json={
        "graph": TWO_PHASE_GRAPH,
        "evaluator": {"mode": "hardware_sim", "noise": 0.01, "averages": 5, "safety": True}})
    assert r.status_code == 200
    assert r.json()["objectives"]["y1"] > 0.95


def test_benches_lists_scenarios():
    d = client.get("/benches").json()["benches"]
    names = {b["name"] for b in d}
    assert {"single_peak", "multi_peak", "skew_peak",
            "rosenbrock", "rastrigin", "ackley"} <= names
    assert all(b["label"] and b["desc"] for b in d)


def test_vocs_exposes_channel_cost():
    objs = client.get("/vocs").json()["objectives"]
    assert objs["y1"]["cost"] == 1.0 and objs["y1"]["device"] == "光功率计"
    assert objs["y2"]["cost"] == 2.0


def test_surface_returns_grid():
    r = client.post("/surface", json={"bench": "single_peak", "objective": "y1",
                                      "xvar": "x1", "yvar": "x2", "nx": 11, "ny": 9})
    assert r.status_code == 200
    d = r.json()
    assert len(d["xs"]) == 11 and len(d["ys"]) == 9
    assert len(d["z"]) == 9 and len(d["z"][0]) == 11
    # the sampled surface must contain the near-1.0 peak somewhere
    assert max(max(row) for row in d["z"]) > 0.9


def test_autotune_space_lists_phases():
    d = client.get("/autotune/space").json()["phases"]
    assert set(d) == {"coarse", "refine", "fit"}
    coarse = {a["name"] for a in d["coarse"]}
    assert "grid_scan" in coarse and "bayesian" in coarse       # bayesian is coarse
    assert all("default_variation" in a for a in d["refine"])


def test_autotune_endpoint_ranks():
    r = client.post("/autotune", json={"bench": "single_peak", "n_trials": 1,
                                       "noise_levels": [0.0], "max_candidates": 5})
    assert r.status_code == 200
    d = r.json()
    assert d["n_candidates"] > 0 and d["ranked"][0]["quality"] > 0.9


def test_run_reports_cost_and_fits():
    r = client.post("/run/graph", json={"graph": TWO_PHASE_GRAPH,
                                        "evaluator": {"mode": "function"}})
    d = r.json()
    assert d["sim_seconds"] > 0 and "y1" in d["reads"]
    assert isinstance(d["fits"], dict)


def test_run_graph_multi_peak_bench():
    # the multi-peak bench runs and the workflow climbs *a* lobe (may be a
    # sidelobe — that is the scenario's point: naive local search can stick).
    r = client.post("/run/graph", json={"graph": TWO_PHASE_GRAPH,
                                        "evaluator": {"mode": "function", "bench": "multi_peak"}})
    assert r.status_code == 200
    assert r.json()["objectives"]["y1"] > 0.7


def test_safety_limits_override_clamps():
    # cap x1 at 0 via safety_limits; the peak at x1=2 is unreachable -> y1 stays low
    graph = {"nodes": [{"id": "n", "type": "algorithm", "data": {
        "algorithm": "coordinate_descent", "variables": ["x1", "x2"], "objective": "y1",
        "stop": {"max_iter": 100}}}], "edges": []}
    r = client.post("/run/graph", json={"graph": graph, "evaluator": {
        "mode": "hardware_sim", "safety": True,
        "safety_limits": {"x1": [-2.0, 0.0], "x2": [-5.0, 3.0]}}})
    assert r.status_code == 200
    assert r.json()["objectives"]["y1"] < 0.7        # clamped away from the x1=2 peak


def test_bad_algorithm_returns_400():
    r = client.post("/run/graph", json={"graph": {
        "nodes": [{"id": "n", "type": "algorithm",
                   "data": {"algorithm": "nope", "variables": ["x1"], "objective": "y1"}}],
        "edges": []}})
    assert r.status_code == 400


def test_serves_canvas():
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_workspace_get_post_roundtrip():
    sid = "apitest"
    graph = {"nodes": [{"id": "n1", "type": "algorithm",
                        "data": {"algorithm": "grid_scan", "variables": ["x1"], "objective": "y1"}}],
             "edges": []}
    r0 = client.get(f"/workspace/{sid}").json()
    r1 = client.post(f"/workspace/{sid}", json={"graph": graph, "bench": "multi_peak"}).json()
    assert r1["revision"] == r0["revision"] + 1
    r2 = client.get(f"/workspace/{sid}").json()
    assert r2["bench"] == "multi_peak"
    assert r2["graph"]["nodes"][0]["data"]["algorithm"] == "grid_scan"
