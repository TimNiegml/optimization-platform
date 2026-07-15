"""Headless runs of the demo pipelines.

  python run_demo.py            # two-phase: find-light -> optimise (default)
  python run_demo.py loop       # alternating loop with keep-constraint + fallback
"""
import sys

from optplat import Evaluator, Orchestrator
from optplat.demo import DEMO_PIPELINE, TWO_PHASE_PIPELINE, demo_vocs, optical_bench


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "two_phase"
    pipeline = DEMO_PIPELINE if which == "loop" else TWO_PHASE_PIPELINE

    evaluator = Evaluator(optical_bench)
    result = Orchestrator(demo_vocs(), evaluator, pipeline).run()

    print("\n".join(result["events"]))
    print("\n=== RESULT ===")
    print("operating point:", {k: round(v, 3) for k, v in result["state"].items()})
    print("objectives     :", result["objectives"])
    print("evaluations    :", result["n_evals"])


if __name__ == "__main__":
    main()
