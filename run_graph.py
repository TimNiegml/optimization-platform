"""Run a workflow authored as a node+edge graph JSON (Dify-style).

    python run_graph.py [graph.json]

Loads the graph JSON, prints it as a mermaid diagram (a stand-in for the canvas
render), then executes it directly — no compile step, the JSON is the artifact.
"""
import json
import sys

from optplat import Evaluator, GraphRunner, to_mermaid
from optplat.demo import demo_vocs, optical_bench


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "workflow_graph_example.json"
    graph = json.load(open(path, encoding="utf-8"))

    print("=== graph (mermaid; paste into any mermaid viewer / the canvas) ===")
    print(to_mermaid(graph))

    print("\n=== executing graph JSON directly ===")
    result = GraphRunner(demo_vocs(), Evaluator(optical_bench), graph).run()
    for e in result["events"]:
        print("  " + e)
    print("\noperating point:", {k: round(v, 3) for k, v in result["state"].items()})
    print("objectives     :", result["objectives"])
    print("evaluations    :", result["n_evals"])


if __name__ == "__main__":
    main()
