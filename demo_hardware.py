"""End-to-end hardware + persistence demo (no real instruments needed).

    python demo_hardware.py

Shows: simulated stage + noisy power meter, N-read averaging, independent
safety limits, SQLite archiving, then RESUME from the best archived point and
one-click ROLLBACK driving the stage back to that point.
"""
from optplat import (
    HardwareEvaluator,
    Orchestrator,
    SafetyLimits,
    SimulatedMeter,
    SimulatedStage,
    SQLiteStore,
    rollback_to_best,
)
from optplat.demo import TWO_PHASE_PIPELINE, demo_vocs, optical_bench


def line(title):
    print("\n" + "=" * 60 + f"\n{title}\n" + "=" * 60)


def main():
    vocs = demo_vocs()

    line("1) 接入'硬件'：模拟电机台 + 噪声功率计 + 安全限位 + SQLite 归档")
    stage = SimulatedStage(vocs.initial_point())
    meter = SimulatedMeter(stage, optical_bench, noise=0.01, seed=0)   # 1% 噪声
    safety = SafetyLimits({"x1": (-3, 7), "x2": (-6, 4), "x3": (0, 1.5)})
    store = SQLiteStore("demo_run.db", run_id="joblot-42")
    ev = HardwareEvaluator(stage, meter, settle_time=0.0, averages=5, safety=safety,
                           store=store)
    print("电机台起点:", {k: round(v, 2) for k, v in stage.pos.items()})
    print("配置       : 每点平均 5 次, 安全限位已启用, 归档到 demo_run.db")

    line("2) 跑两阶段流程：找光(grid) → 精调(Nelder-Mead) → 均衡(公式法)")
    result = Orchestrator(vocs, ev, TWO_PHASE_PIPELINE).run()
    for e in result["events"]:
        print("  " + e)
    print("\n结果: y1=%.3f  y2=%.3f  评估 %d 次" % (
        result["objectives"]["y1"], result["objectives"]["y2"], result["n_evals"]))
    print("最优点:", {k: round(v, 3) for k, v in result["state"].items()})
    print("已归档:", len(store.history()), "条评估")

    line("3) 断点续跑：从归档的最优点继续（换个新进程也能这样重开）")
    store2 = SQLiteStore("demo_run.db", run_id="joblot-42")
    best_pt, best_obj = store2.best("y1", "max")
    print("归档最优 y1 点:", {k: round(v, 3) for k, v in best_pt.items()},
          " y1=%.3f" % best_obj["y1"])
    orch = Orchestrator(vocs, ev, TWO_PHASE_PIPELINE, start_point=best_pt)
    print("续跑 orchestrator 起点:", {k: round(v, 3) for k, v in orch.state.items()})

    line("4) 一键回滚：电机漂走后，把台子开回历史最优点")
    stage.move("x1", -2.0); stage.move("x2", 3.0)
    print("电机漂到:", {k: round(v, 2) for k, v in stage.pos.items()})
    back = rollback_to_best(ev, store2, "y1", "max")
    print("回滚后到:", {k: round(v, 2) for k, v in stage.pos.items()}, "(= 历史最优点)")


if __name__ == "__main__":
    main()
