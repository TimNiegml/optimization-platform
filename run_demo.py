"""Headless run of the demo: the exact '先优 y1 → 再优 y2 保持 y1>k' example."""
from optplat import Evaluator, Orchestrator
from optplat.demo import DEMO_PIPELINE, demo_vocs, optical_bench


def main() -> None:
    vocs = demo_vocs()
    evaluator = Evaluator(optical_bench)
    orch = Orchestrator(vocs, evaluator, DEMO_PIPELINE)
    result = orch.run()

    print("\n".join(result["events"]))
    print("\n=== RESULT ===")
    print("operating point:", {k: round(v, 3) for k, v in result["state"].items()})
    print("objectives     :", result["objectives"])
    print("evaluations    :", result["n_evals"])


if __name__ == "__main__":
    main()
