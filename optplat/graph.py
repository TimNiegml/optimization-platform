"""GraphRunner = the node+edge (Dify-style) driver.

Executes a workflow authored as a graph JSON — the shape a drag-drop canvas
(React Flow) naturally emits — directly as a bounded state machine, reusing the
shared StageEngine. No compile step: the JSON IS the runnable artifact.

Graph JSON:
    {
      "nodes": [
        {"id": "start", "type": "start"},
        {"id": "find",  "type": "algorithm",
         "data": {"algorithm": "grid_scan", "variables": ["x1","x2"],
                  "objective": "y1", "stop": {"target": "y1 > 0.2"}}},
        ...
        {"id": "end", "type": "end"}
      ],
      "edges": [
        {"source": "start", "target": "find"},
        {"source": "refine", "target": "balance",
         "condition": "not (y1>=0.95 and y2>=0.9)"},   # conditional edge (loop/branch)
        {"source": "refine", "target": "end"}          # default edge (no condition)
      ],
      "until": "y1>=0.98 and y2>=0.95"                  # optional global early-stop
    }

Control flow:
  * branch  — a node's outgoing edges are tried in order; the first edge whose
              `condition` is true wins, else the first edge with no condition.
  * loop    — an edge may point back to an earlier node; the loop runs while its
              `condition` holds. Bounded by per-node `max_visits` and a global
              `max_steps` — no infinite loops (no runaway motors).
  * for     — a `for_loop` node has `data.iterations` and exactly one outgoing
              `role: body` edge plus one `role: exit` edge. The body tail points
              back to the loop node; the global `max_steps` remains a hard stop.
"""
from __future__ import annotations

from typing import Optional

from .engine import StageEngine, StopAll
from .evaluator import Evaluator
from .transforms import SCALAR_OPERATIONS, TransformEvaluator
from .vocs import VOCS, Objective, ObjectiveMode, ObjectiveValueType


class GraphRunner:
    def __init__(self, vocs: VOCS, evaluator: Evaluator, graph: dict,
                 eval_budget: int = 5000, start_point: Optional[dict] = None,
                 max_steps: int = 500, default_max_visits: int = 20):
        self.graph = graph
        transforms = [n.get("data", {}) for n in graph["nodes"]
                      if n.get("type") == "data_transform"]
        if transforms:
            vocs = vocs.model_copy(deep=True)
            known = set(vocs.objectives)
            for spec in transforms:
                output = spec.get("output")
                if output and output not in known:
                    scalar = spec.get("operation") in SCALAR_OPERATIONS
                    vocs.objectives[output] = Objective(
                        mode=ObjectiveMode.SCAN,
                        value_type=ObjectiveValueType.SCALAR if scalar else ObjectiveValueType.MATRIX)
                    known.add(output)
            evaluator = TransformEvaluator(evaluator, transforms, set(vocs.objectives) - set(s.get("output") for s in transforms))
        self.engine = StageEngine(vocs, evaluator, eval_budget, start_point)
        self.engine.global_until = graph.get("until")
        self.max_steps = max_steps
        self.default_max_visits = default_max_visits
        self._nodes = {n["id"]: n for n in graph["nodes"]}
        self._out: dict[str, list] = {}
        for e in graph["edges"]:
            self._out.setdefault(e["source"], []).append(e)

    @property
    def state(self) -> dict[str, float]:
        return self.engine.state

    def _start_node(self) -> str:
        for n in self.graph["nodes"]:
            if n.get("type") == "start":
                return n["id"]
        targets = {e["target"] for e in self.graph["edges"]}
        for n in self.graph["nodes"]:                 # first node with no incoming edge
            if n["id"] not in targets:
                return n["id"]
        return self.graph["nodes"][0]["id"]

    def _next(self, node_id: str) -> Optional[str]:
        out = self._out.get(node_id, [])
        for e in out:                                 # conditional edges first, in order
            if e.get("condition") and self.engine.cond(e["condition"]):
                return e["target"]
        for e in out:                                 # then the default (unconditional) edge
            if not e.get("condition"):
                return e["target"]
        return None                                   # no outgoing edge -> implicit end

    def _next_for(self, node_id: str, visit: int, iterations: int) -> Optional[str]:
        """Choose the explicit body/exit branch of a bounded for-loop node."""
        out = self._out.get(node_id, [])
        role = "body" if visit <= iterations else "exit"
        edge = next((e for e in out if e.get("role") == role), None)
        if edge is None:
            raise ValueError(f"for_loop {node_id!r} needs one {role!r} edge")
        return edge["target"]

    def run(self) -> dict:
        eng = self.engine
        eng.prime()
        visits: dict[str, int] = {}
        cur = self._start_node()
        steps = 0
        try:
            while cur is not None and steps < self.max_steps:
                steps += 1
                node = self._nodes[cur]
                ntype = node.get("type", "algorithm")
                if ntype == "start":
                    cur = self._next(cur)
                    continue
                if ntype == "end":
                    eng.events.append("■ end")
                    break
                visits[cur] = visits.get(cur, 0) + 1
                if ntype == "for_loop":
                    iterations = int(node.get("data", {}).get("iterations", 1))
                    if iterations < 0 or iterations > 1000:
                        raise ValueError("for_loop iterations must be between 0 and 1000")
                    cur = self._next_for(cur, visits[cur], iterations)
                    continue
                mv = node.get("max_visits", self.default_max_visits)
                if visits[cur] > mv:
                    eng.events.append(f"   node '{cur}' hit max_visits={mv} → stop")
                    break
                if ntype == "observer":
                    eng.observe(cur, node.get("data", {}))
                elif ntype == "data_transform":
                    data = node.get("data", {})
                    eng.observe(cur, {"label": data.get("label", data.get("output", cur)),
                                      "kind": "transform", "channels": [data["output"]]})
                elif ntype == "algorithm":
                    eng.run_stage(node["data"])
                else:
                    raise ValueError(f"unsupported graph node type: {ntype!r}")
                cur = self._next(cur)
            if steps >= self.max_steps:
                eng.events.append(f"⛔ max_steps={self.max_steps} reached → stop")
        except StopAll:
            pass
        return eng.result()


def to_mermaid(graph: dict) -> str:
    """Render a graph JSON as a mermaid flowchart (a stand-in for the canvas)."""
    lines = ["flowchart TD"]
    for n in graph["nodes"]:
        nid = n["id"]
        t = n.get("type", "algorithm")
        if t == "start":
            lines.append(f'  {nid}([start])')
        elif t == "end":
            lines.append(f'  {nid}([end])')
        elif t == "observer":
            d = n.get("data", {})
            lines.append(f'  {nid}{{"👁 {d.get("label", nid)}<br/>{d.get("kind", "scalar")}"}}')
        elif t == "for_loop":
            d = n.get("data", {})
            lines.append(f'  {nid}{{"for × {d.get("iterations", 1)}"}}')
        elif t == "data_transform":
            d = n.get("data", {})
            lines.append(f'  {nid}["⇄ {d.get("input", "?")} → {d.get("operation", "identity")} → {d.get("output", "?")}"]')
        else:
            d = n.get("data", {})
            label = f'{nid}: {d.get("algorithm","?")}<br/>{",".join(d.get("variables",[]))} → {d.get("objective","")}'
            lines.append(f'  {nid}["{label}"]')
    for e in graph["edges"]:
        cond = e.get("condition") or e.get("role")
        if cond:
            lines.append(f'  {e["source"]} -->|{cond}| {e["target"]}')
        else:
            lines.append(f'  {e["source"]} --> {e["target"]}')
    return "\n".join(lines)
