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
    assert {"single_peak", "multi_peak", "skew_peak"} <= names
    assert all(b["label"] and b["desc"] for b in d)


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
