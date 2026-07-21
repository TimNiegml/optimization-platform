"""Run an optimisation workflow against an EXTERNAL user device — no API needed.

    python run_device.py examples/device_template.py                # default demo flow
    python run_device.py path/to/your_device.py my_graph.json       # your graph IR

Your device module defines axes (x) + meters (y); see examples/device_template.py
and optplat/userdev.py. The optimisation starts from the axes' CURRENT positions
(axis.get()), i.e. wherever the hardware already is — no absolute origin assumed.
"""
import json
import sys

from optplat.graph import GraphRunner
from optplat.userdev import load_device


def _default_graph(vocs) -> dict:
    """A generic find-light → refine flow over the first 1-2 axes and first meter,
    using a RELATIVE grid scan (span_frac) so it scans around the current position."""
    xs = list(vocs.variables)[:2]
    y = list(vocs.objectives)[0]
    return {
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "scan", "type": "algorithm", "data": {
                "algorithm": "grid_scan", "variables": xs, "objective": y,
                "n_per_axis": 7, "span_frac": 0.5, "stop": {"target": f"{y} > 0.2"}}},
            {"id": "refine", "type": "algorithm", "data": {
                "algorithm": "nelder_mead", "variables": xs, "objective": y}},
            {"id": "end", "type": "end"},
        ],
        "edges": [{"source": "start", "target": "scan"},
                  {"source": "scan", "target": "refine"},
                  {"source": "refine", "target": "end"}],
    }


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    dev = load_device(argv[1])
    vocs = dev.vocs()
    print(f"设备：{dev.info()['n_axes']} 个自变量 x = {list(vocs.variables)}，"
          f"{dev.info()['n_meters']} 个因变量 y = {list(vocs.objectives)}")
    start = dev.current_point()
    print(f"起点（轴当前位置）：{ {k: round(v, 3) for k, v in start.items()} }")

    graph = json.load(open(argv[2], encoding="utf-8")) if len(argv) > 2 else _default_graph(vocs)
    ev = dev.evaluator()
    res = GraphRunner(vocs, ev, graph, start_point=start).run()

    print("\n=== 结果 ===")
    print("最终 y：", {k: round(v, 5) for k, v in res["objectives"].items()})
    print("最优 x：", {k: round(v, 4) for k, v in res["state"].items() if k in vocs.variables})
    print(f"评估 {res['n_evals']} 次，测量耗时 {res.get('sim_seconds', 0):.1f}s，读取 {res.get('reads', {})}")
    for st, f in (res.get("fits") or {}).items():
        print(f"  拟合[{st}]：{f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
