"""AutoTuner (L1) + ModelProvider tests."""
from optplat.autotune import (
    TuneSpec,
    demonstrate_candidate,
    default_variation,
    generate_candidates,
    phase_algorithms,
    run_autotune,
)
from optplat.demo import optical_bench
from optplat.generators import CoordinateDescent
from optplat.models import build_model, register_model
from optplat.registry import AlgorithmSpec, register_algorithm


def test_generate_candidates_are_three_phase_graphs():
    spec = TuneSpec(max_candidates=10)
    cands = generate_candidates(spec)
    assert 0 < len(cands) <= 10
    for c in cands:
        g = c["graph"]
        assert g["nodes"] and "until" in g            # target -> global until
        # nodes chain via edges; at least one algorithm node
        assert all(n["type"] == "algorithm" for n in g["nodes"])
        assert c["label"]


def test_variation_is_registry_driven():
    # variation 档位 auto-derives from each algorithm's param schema
    gv = default_variation("grid_scan")
    assert {} in gv and any("n_per_axis" in v for v in gv)
    # a NEWLY registered algorithm auto-joins its phase and appears in candidates
    register_algorithm(AlgorithmSpec(
        "at_new_local", "local", False,
        lambda v, s: CoordinateDescent(v, s["variables"], s["objective"]),
        {"gain": {"type": "float", "default": 1.0, "min": 0.5, "max": 2.0}},
        label="调优新算子"))
    assert "at_new_local" in phase_algorithms()["refine"]
    assert any({"gain": 0.5} == v or {"gain": 2.0} == v for v in default_variation("at_new_local"))
    cands = generate_candidates(TuneSpec(max_candidates=60))
    assert any("at_new_local" in str(c["graph"]) for c in cands)


def test_variation_override_respected():
    spec = TuneSpec(max_candidates=40, allow_coarse=["grid_scan"], allow_refine=[], allow_fit=[],
                    balance_obj=None, variation={"grid_scan": [{"n_per_axis": 5}]})
    cands = generate_candidates(spec)
    # only grid_scan with the overridden param appears
    for c in cands:
        for node in c["graph"]["nodes"]:
            assert node["data"]["algorithm"] == "grid_scan"
            assert node["data"].get("n_per_axis") == 5


def test_run_autotune_ranks_and_scores():
    spec = TuneSpec(bench="single_peak", n_trials=1, noise_levels=[0.0], max_candidates=6)
    res = run_autotune(spec)
    assert res["n_candidates"] == len(res["ranked"]) > 0
    top = res["ranked"][0]
    # utility-sorted descending
    utils = [r["utility"] for r in res["ranked"]]
    assert utils == sorted(utils, reverse=True)
    # each candidate carries the scored axes + a runnable graph
    for r in res["ranked"]:
        assert {"quality", "time", "stability", "utility", "pareto", "graph"} <= set(r)
    assert any(r["pareto"] for r in res["ranked"])
    assert top["quality"] > 0.9                       # best workflow solves single_peak


def test_candidate_demo_replays_each_seed_with_history_and_summary():
    spec = TuneSpec(bench="single_peak", n_trials=2, noise_levels=[0.0, 0.02],
                    max_candidates=2, seed=11, random_start=True)
    ranked = run_autotune(spec)["ranked"]
    demo = demonstrate_candidate(spec, ranked[0]["graph"])
    assert len(demo["cases"]) == 4
    assert demo["summary"]["n_cases"] == 4
    assert demo["summary"]["n_success"] == 4
    assert all(c["history"] and c["start"] and c["final_state"] for c in demo["cases"])
    assert {c["seed"] for c in demo["cases"]} == {11, 18, 31, 38}
    assert 0 <= demo["summary"]["reached_rate"] <= 1


def test_weights_shift_ranking_toward_speed():
    # heavily weighting time should favour a lower-time candidate at the top
    fast = run_autotune(TuneSpec(n_trials=1, noise_levels=[0.0], max_candidates=8,
                                 weights={"quality": 1.0, "time": 5.0, "stability": 0.5}))
    quality = run_autotune(TuneSpec(n_trials=1, noise_levels=[0.0], max_candidates=8,
                                    weights={"quality": 5.0, "time": 0.0, "stability": 1.0}))
    # the speed-weighted run's winner is no slower than the quality-weighted run's winner
    assert fast["ranked"][0]["time"] <= quality["ranked"][0]["time"] + 1e-9


def test_stability_from_noise_trials():
    # with a target and noise, stability is the reached-target rate in [0,1]
    res = run_autotune(TuneSpec(bench="single_peak", n_trials=2, noise_levels=[0.0, 0.03],
                                max_candidates=5, target="y1>=0.95 and y2>=0.9"))
    for r in res["ranked"]:
        assert 0.0 <= r["stability"] <= 1.0


def test_model_provider_analytic_and_dataset():
    fa = build_model({"kind": "analytic", "bench": "single_peak"})
    assert abs(fa({"x1": 2.0, "x2": -1.0, "x3": 0.6})["y1"] - 1.0) < 1e-6
    data = [{"x1": 2.0, "x2": -1.0, "x3": 0.6, "y1": 1.0, "y2": 1.0},
            {"x1": 0.0, "x2": 0.0, "x3": 0.0, "y1": 0.1, "y2": 0.0}]
    fd = build_model({"kind": "dataset_idw", "data": data,
                      "variables": ["x1", "x2", "x3"], "objectives": ["y1", "y2"]})
    # exact sample point returns its measured value
    assert abs(fd({"x1": 2.0, "x2": -1.0, "x3": 0.6})["y1"] - 1.0) < 1e-9


def test_autotune_with_dataset_model():
    import random
    random.seed(1)
    data = []
    for _ in range(300):
        x = {"x1": random.uniform(-2, 6), "x2": random.uniform(-5, 3), "x3": random.uniform(0, 1.5)}
        data.append({**x, **optical_bench(x)})
    spec = TuneSpec(model={"kind": "dataset_idw", "data": data,
                           "variables": ["x1", "x2", "x3"], "objectives": ["y1", "y2"]},
                    n_trials=1, noise_levels=[0.0], max_candidates=4, target=None)
    res = run_autotune(spec)                          # surrogate drives the same search
    assert res["n_candidates"] > 0 and res["ranked"][0]["quality"] > 0
