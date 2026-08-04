"""API tests via FastAPI TestClient (needs httpx). Skipped if httpx absent."""
import sys

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


def test_bad_algorithm_returns_400():
    r = client.post("/run/graph", json={"graph": {
        "nodes": [{"id": "n", "type": "algorithm",
                   "data": {"algorithm": "nope", "variables": ["x1"], "objective": "y1"}}],
        "edges": []}})
    assert r.status_code == 400


ZEMAX_CONN = {"command": [sys.executable, "-m", "optplat.zemax_sim", "--pickups"],
              "mode": "extension"}


def test_catalog_lists_evaluator_backends():
    assert {"function", "hardware_sim", "zemax", "composite"} <= set(
        client.get("/backends").json()["backends"])


def test_zemax_inspect_returns_pickable_variables():
    """The canvas reads the design and offers surfaces/variables to choose from."""
    r = client.post("/zemax/inspect", json={"connection": ZEMAX_CONN})
    assert r.status_code == 200
    d = r.json()
    assert d["system"]["units"] == "mm"
    tilt = next(c for c in d["choices"] if c["id"] == "s3.tilt_x")
    assert tilt["label"] == "S3 · align in · Tilt About X" and tilt["unit"] == "deg"
    # the design's own pickup solve shows up as a follower of the picked cell
    assert [f["surface"] for f in tilt["followers"]] == [5]
    assert {op["type"] for op in d["operands"]} == {"DMFS", "RSCE", "TTHI"}


def test_zemax_binding_from_selections():
    r = client.post("/zemax/binding", json={
        "connection": ZEMAX_CONN,
        "selections": [{"name": "tilt_x", "surface": 3, "param": "tilt_x",
                        "low": -1, "high": 1}],
        "merit": {"total": "merit"}})
    assert r.status_code == 200
    d = r.json()
    assert d["binding"]["knobs"]["tilt_x"]["followers"][0]["surface"] == 5
    assert d["vocs"]["objectives"]["merit"]["mode"] == "minimize"


def test_run_graph_against_zemax_backend():
    """The exact request the canvas sends after picking surfaces on the design."""
    picks = client.post("/zemax/inspect", json={"connection": ZEMAX_CONN}).json()
    knobs, variables = {}, []
    for pid in ("s3.decenter_x", "s3.decenter_y", "s3.tilt_x", "s3.tilt_y"):
        c = next(x for x in picks["choices"] if x["id"] == pid)
        name = c["param"] + "_s" + str(c["surface"])
        variables.append(name)
        knobs[name] = {"surface": c["surface"], "kind": c["kind"], "param": c["param"],
                       "low": -1.0, "high": 1.0, "followers": c["followers"]}

    r = client.post("/run/graph", json={
        "vocs": {"variables": {n: {"low": -1.0, "high": 1.0} for n in variables},
                 "objectives": {"merit": {"mode": "minimize"}}},
        "evaluator": {"mode": "zemax", "connection": ZEMAX_CONN,
                      "binding": {"knobs": knobs, "merit": {"total": "merit"},
                                  "refresh": {"system": True, "followers": True,
                                              "pupil": True}}},
        "graph": {"until": "merit < 0.05", "nodes": [
            {"id": "start", "type": "start"},
            {"id": "align", "type": "algorithm", "data": {
                "algorithm": "coordinate_descent", "variables": variables,
                "objective": "merit", "step": 0.4, "stop": {"max_iter": 300}}},
            {"id": "end", "type": "end"}], "edges": [
            {"source": "start", "target": "align"}, {"source": "align", "target": "end"}]}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["objectives"]["merit"] < 0.05
    # follower cells were verified and archived alongside x and y
    assert d["history"][-1]["s5.p3"] == pytest.approx(-d["history"][-1]["tilt_x_s3"])


def test_zemax_inspect_bad_connection_returns_400():
    r = client.post("/zemax/inspect", json={"connection": {"command": ["/nonexistent"]}})
    assert r.status_code == 400


def test_serves_canvas():
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
