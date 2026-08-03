"""Benchmark suites — the DEV set you tune on, and the FROZEN set that judges you.

The whole point of an audit is that the thing being measured cannot influence the
measuring stick. So there are two suites:

  * **DEV**   — visible. Tune, diagnose and iterate against this freely.
  * **FROZEN** — the reality anchor. Different start points, different landscapes,
    different noise. Whoever designs algorithms (human or LLM) never tunes on it;
    it is only ever used for final acceptance.

`FrozenSuite.fingerprint()` is a content hash over the canonical problem list. Any
edit changes the hash, so "the frozen set was quietly changed to make the numbers
look good" is detectable rather than a matter of trust. Record the fingerprint
alongside every audit result.

A problem is deliberately small and declarative:
    {id, bench, start_point|None, noise, averages, target, quality_obj, eval_budget}
`start_point=None` means "random start from a seeded draw", which is the honest
setting for hardware that has no absolute origin.
"""
from __future__ import annotations

import hashlib
import json
import random
from typing import Optional

from pydantic import BaseModel, Field


class Problem(BaseModel):
    id: str
    bench: str = "single_peak"
    start_point: Optional[dict[str, float]] = None   # None = seeded random start
    noise: float = 0.0
    averages: int = 1
    target: Optional[str] = None                      # 达标判据, e.g. "y1>=0.95"
    quality_obj: str = "y1"
    eval_budget: int = 800
    seed: int = 0
    note: str = ""


class BenchSuite(BaseModel):
    name: str
    kind: str = "dev"                                 # "dev" | "frozen"
    problems: list[Problem] = Field(default_factory=list)

    def fingerprint(self) -> str:
        """Content hash — proves the suite used in an audit is the one on record."""
        payload = json.dumps(
            [p.model_dump() for p in self.problems],
            sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict:
        return {"name": self.name, "kind": self.kind, "n_problems": len(self.problems),
                "fingerprint": self.fingerprint(),
                "benches": sorted({p.bench for p in self.problems}),
                "problem_ids": [p.id for p in self.problems]}


def _rand_start(seed: int, spread: float = 0.8) -> dict[str, float]:
    """A reproducible start point away from the demo VOCS centre (which happens to
    sit exactly on the single_peak optimum — starting there would flatter every
    algorithm and measure nothing)."""
    rng = random.Random(seed)
    bounds = {"x1": (-2.0, 6.0), "x2": (-5.0, 3.0), "x3": (0.0, 1.5)}
    pt = {}
    for k, (lo, hi) in bounds.items():
        mid = 0.5 * (lo + hi)
        half = 0.5 * (hi - lo) * spread
        pt[k] = round(rng.uniform(mid - half, mid + half), 4)
    return pt


# ---------------------------------------------------------------- built-in suites
# DEV: visible, for tuning/diagnosis. Mainstream landscapes, mild noise.
DEV_SUITE = BenchSuite(
    name="dev_v1", kind="dev",
    problems=[
        Problem(id="dev_single", bench="single_peak", start_point=_rand_start(11),
                target="y1>=0.95", seed=11, note="标准单峰找光"),
        Problem(id="dev_single_noisy", bench="single_peak", start_point=_rand_start(12),
                noise=0.02, averages=3, target="y1>=0.9", seed=12, note="带噪单峰"),
        Problem(id="dev_multi", bench="multi_peak", start_point=_rand_start(13),
                target="y1>=0.9", seed=13, note="多峰，考验是否卡旁瓣"),
        Problem(id="dev_skew", bench="skew_peak", start_point=_rand_start(14),
                target="y1>=0.9", seed=14, note="偏斜相关峰"),
    ])

# FROZEN: the acceptance set. Deliberately NOT the same landscapes/starts as DEV —
# harder, more varied, includes noise and the classic hard functions. Never tune here.
FROZEN_SUITE = BenchSuite(
    name="frozen_v1", kind="frozen",
    problems=[
        Problem(id="frz_single_far", bench="single_peak", start_point={"x1": 5.6, "x2": 2.6, "x3": 1.4},
                target="y1>=0.95", seed=101, note="起点在角落，远离峰"),
        Problem(id="frz_single_noisy", bench="single_peak", start_point=_rand_start(102),
                noise=0.04, averages=3, target="y1>=0.85", seed=102, note="强噪单峰"),
        Problem(id="frz_multi_far", bench="multi_peak", start_point={"x1": -1.8, "x2": -4.5, "x3": 0.2},
                target="y1>=0.9", seed=103, note="多峰且起点贴着旁瓣"),
        Problem(id="frz_rosenbrock", bench="rosenbrock", start_point=_rand_start(104),
                target="y1>=0.85", seed=104, note="相关谷，逐轴下降会很慢"),
        Problem(id="frz_rastrigin", bench="rastrigin", start_point=_rand_start(105),
                target="y1>=0.8", seed=105, note="强多峰"),
        Problem(id="frz_ackley", bench="ackley", start_point=_rand_start(106),
                target="y1>=0.8", seed=106, note="外围平坦+中心尖峰"),
        Problem(id="frz_skew_noisy", bench="skew_peak", start_point=_rand_start(107),
                noise=0.03, averages=3, target="y1>=0.85", seed=107, note="带噪偏斜峰"),
    ])

SUITES: dict[str, BenchSuite] = {s.name: s for s in (DEV_SUITE, FROZEN_SUITE)}


def get_suite(name_or_kind: str) -> BenchSuite:
    """Fetch by suite name ('dev_v1') or by kind ('dev' / 'frozen')."""
    if name_or_kind in SUITES:
        return SUITES[name_or_kind]
    for s in SUITES.values():
        if s.kind == name_or_kind:
            return s
    raise KeyError(f"unknown suite: {name_or_kind} (have {sorted(SUITES)})")


def register_suite(suite: BenchSuite) -> None:
    """Add a custom suite (e.g. one built from real-hardware history)."""
    SUITES[suite.name] = suite
