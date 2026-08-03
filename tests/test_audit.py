"""Audit stack tests: trace digest, frozen suite, generalization gap, search space.

The invariants these lock down are the ones that keep the loop honest:
  * a defect must REPRODUCE across seeds before it is reported as reliable
  * the frozen suite's fingerprint changes if anyone edits it
  * ranking uses FROZEN only; DEV never contributes to the ranking
  * an LLM-proposed search range cannot escape the parameter's declared schema
"""
import optplat.audit as audit_mod
from optplat.audit import audit_solution, compare_solutions, run_problem
from optplat.autotune import TuneSpec, _variants, search_space_variants
from optplat.benchsuite import BenchSuite, Problem, get_suite
from optplat.demo import bench_func, demo_vocs
from optplat.evaluator import Evaluator
from optplat.graph import GraphRunner
from optplat.trace_digest import digest, digest_many, length_scales, suggested_search_space


def _wf(nodes):
    ns = [{"id": "s", "type": "start"}] + nodes + [{"id": "e", "type": "end"}]
    ids = [n["id"] for n in ns]
    return {"nodes": ns,
            "edges": [{"source": ids[i], "target": ids[i + 1]} for i in range(len(ids) - 1)]}


CD = _wf([{"id": "a", "type": "algorithm", "data": {
    "algorithm": "coordinate_descent", "variables": ["x1", "x2"], "objective": "y1"}}])
SCAN_NM = _wf([
    {"id": "b1", "type": "algorithm", "data": {
        "algorithm": "grid_scan", "variables": ["x1", "x2"], "objective": "y1",
        "n_per_axis": 9, "stop": {"target": "y1>0.2"}}},
    {"id": "b2", "type": "algorithm", "data": {
        "algorithm": "nelder_mead", "variables": ["x1", "x2"], "objective": "y1"}}])


def _run(graph, start=None, bench="single_peak"):
    vocs = demo_vocs()
    ev = Evaluator(bench_func(bench), costs={"y1": 1.0, "y2": 2.0})
    return GraphRunner(vocs, ev, graph, start_point=start).run()


# ---------------- trace digest ----------------
def test_length_scale_recovers_peak_width():
    # single_peak's lobe has w=1.2, so the half-max region spans ~2-3 units;
    # the estimate must land in that ballpark, not at the full 8-unit range.
    res = _run(SCAN_NM, start={"x1": 5.5, "x2": 2.5, "x3": 0.5})
    scales = length_scales(res["history"], demo_vocs(), "y1")
    assert 0.5 < scales["x1"]["peak_width"] < 4.0
    assert scales["x1"]["suggested_step"] < scales["x1"]["peak_width"]


def test_digest_attributes_gain_to_the_stage_that_earned_it():
    # grid_scan stops at first light (stop.target y1>0.2), so the refine stage
    # should own most of the gain — attribution must reflect that split, and the
    # shares must account for the whole improvement.
    res = _run(SCAN_NM, start={"x1": 5.5, "x2": 2.5, "x3": 0.1})
    d = digest(res, demo_vocs(), "y1")
    by_stage = {r["stage"]: r for r in d["stage_attribution"]}
    assert by_stage["nelder_mead"]["gain_share"] > by_stage["grid_scan"]["gain_share"]
    assert abs(sum(r["gain_share"] for r in d["stage_attribution"]) - 1.0) < 1e-6
    assert by_stage["init"]["gain_share"] == 0.0
    assert d["suggested_search_space"]["step_frac"]["low"] > 0


def test_digest_does_not_blame_stages_when_nothing_could_improve():
    # demo VOCS centre sits exactly on single_peak's optimum → zero possible gain.
    res = _run(CD)
    d = digest(res, demo_vocs(), "y1")
    assert all("全局无改善" in r["verdict"] for r in d["stage_attribution"])


def test_digest_many_separates_reproducible_defects_from_noise():
    vocs = demo_vocs()
    from optplat.hardware import HardwareEvaluator, SimulatedMeter, SimulatedStage
    results = []
    for seed in range(5):
        st = SimulatedStage(vocs.initial_point())
        mt = SimulatedMeter(st, bench_func("single_peak"), noise=0.02, seed=seed)
        results.append(GraphRunner(vocs, HardwareEvaluator(st, mt), CD,
                                   start_point={"x1": 5.5, "x2": 2.5, "x3": 0.1}).run())
    m = digest_many(results, vocs, "y1")
    assert m["n_runs"] == 5
    for f in m["issues"]:
        assert 0.0 < f["confidence"] <= 1.0
        # reliability requires a majority of runs — a 1/5 fluke must not qualify
        assert f["reliable"] == (f["confidence"] >= 0.5)
    assert any(f["reliable"] for f in m["issues"])


def test_suggested_search_space_is_bounded():
    ss = suggested_search_space({"x1": {"peak_width": 2.0, "peak_width_frac": 0.25,
                                        "suggested_step": 0.5, "suggested_step_frac": 0.0625,
                                        "range": [-2, 6]}})
    for key in ("step_frac", "span_frac"):
        assert 0 < ss[key]["low"] < ss[key]["high"] <= 1.0


# ---------------- frozen suite ----------------
def test_frozen_suite_fingerprint_detects_tampering():
    frozen = get_suite("frozen")
    fp = frozen.fingerprint()
    assert len(fp) == 16 and frozen.fingerprint() == fp        # stable
    tampered = BenchSuite(name="frozen_v1", kind="frozen",
                          problems=list(frozen.problems) + [Problem(id="sneaky")])
    assert tampered.fingerprint() != fp                        # any edit is visible


def test_dev_and_frozen_are_disjoint_problem_sets():
    dev, frozen = get_suite("dev"), get_suite("frozen")
    assert not ({p.id for p in dev.problems} & {p.id for p in frozen.problems})
    # frozen must cover landscapes dev does not, or it tests nothing new
    assert {p.bench for p in frozen.problems} - {p.bench for p in dev.problems}


def test_run_problem_scores_a_crash_as_zero_instead_of_raising():
    bad = {"nodes": [{"id": "a", "type": "algorithm",
                      "data": {"algorithm": "no_such_algo", "variables": ["x1"],
                               "objective": "y1"}}], "edges": []}
    r = run_problem(bad, get_suite("frozen").problems[0])
    assert r["ok"] is False and r["quality"] == 0.0 and r["reached"] is False


# ---------------- audit / generalization gap ----------------
def test_audit_reports_both_suites_and_a_gap():
    rep = audit_solution(SCAN_NM, label="scan+nm")
    assert rep["dev"]["kind"] == "dev" and rep["frozen"]["kind"] == "frozen"
    assert rep["acceptance"] in ("PASS", "FAIL")
    gap = rep["generalization_gap"]["quality"]
    assert gap == round(rep["dev"]["mean_quality"] - rep["frozen"]["mean_quality"], 6)
    # provenance must pin the exact suites the numbers came from
    assert rep["provenance"]["frozen_fingerprint"] == get_suite("frozen").fingerprint()


def test_audit_fails_a_solution_that_misses_the_frozen_bar(monkeypatch):
    monkeypatch.setattr(audit_mod, "MIN_FROZEN_SUCCESS", 0.99)
    rep = audit_solution(CD, label="weak")
    assert rep["acceptance"] == "FAIL"
    assert any(f["flag"] == "frozen_below_bar" for f in rep["flags"])


def test_ranking_uses_frozen_not_dev():
    out = compare_solutions([{"label": "cd", "graph": CD},
                             {"label": "scan_nm", "graph": SCAN_NM}])
    assert out["best"] in ("cd", "scan_nm")
    order = [r["frozen_success"] for r in out["ranking"]]
    passed = [r["acceptance"] == "PASS" for r in out["ranking"]]
    # PASS candidates rank above FAIL ones, then by frozen success rate
    assert passed == sorted(passed, reverse=True)
    for a, b in zip(order, order[1:]):
        if passed[0] == passed[-1]:
            assert a >= b
    assert "FROZEN" in out["ranked_by"]


# ---------------- LLM-proposed search space ----------------
def test_search_space_narrows_the_range_instead_of_blind_extremes():
    blind = _variants(TuneSpec(), "coordinate_descent")
    assert {"step_frac": 1.0} in blind                        # schema extreme by default
    spec = TuneSpec(search_space={"*": {"step_frac": {"low": 0.01, "high": 0.05, "n": 4}}})
    narrowed = _variants(spec, "coordinate_descent")
    vals = [v["step_frac"] for v in narrowed if "step_frac" in v]
    assert len(vals) == 4 and max(vals) <= 0.05               # extremes replaced
    assert {} in narrowed                                      # baseline still present


def test_search_space_cannot_escape_the_declared_schema():
    # an LLM proposing an out-of-range interval gets clamped, never obeyed blindly
    spec = TuneSpec(search_space={"grid_scan": {"n_per_axis": {"low": 1, "high": 999, "n": 3}}})
    vals = [v["n_per_axis"] for v in search_space_variants(spec, "grid_scan")]
    assert vals and min(vals) >= 3 and max(vals) <= 21


def test_search_space_ignores_params_an_algorithm_does_not_have():
    spec = TuneSpec(search_space={"*": {"step_frac": {"low": 0.01, "high": 0.05, "n": 2}}})
    assert search_space_variants(spec, "nelder_mead") == []   # nelder_mead has no step_frac
