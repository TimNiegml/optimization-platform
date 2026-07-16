"""AutoTuner (L1) + ModelProvider tests."""
from optplat.autotune import TuneSpec, generate_candidates, run_autotune
from optplat.demo import optical_bench
from optplat.models import build_model, register_model


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
