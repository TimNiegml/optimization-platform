# 接入自检 · 冒烟脚本与排错对照

**先冒烟，再上优化。** 接入错误和算法问题在台架上的表现很像（"跑飞了""不收敛"），
分开排查能省大量时间。

---

## 1. 冒烟脚本

存成 `smoke_device.py`（放**仓库根目录**，否则 `import optplat` 找不到；或自行设 `PYTHONPATH`），
`python smoke_device.py 你的设备.py` 运行。不动优化算法，只验证契约。

```python
"""设备接入冒烟自检：只验证接口契约，不跑优化。"""
import sys

from optplat.userdev import load_device

dev = load_device(sys.argv[1])
info = dev.info()
print(f"[1] 解析成功：{info['n_axes']} 个 x = {[a['name'] for a in info['axes']]}，"
      f"{info['n_meters']} 个 y = {[m['name'] for m in info['meters']]}")

# --- 轴：当前位置在量程内 + move/get 往返一致 ---
start = dev.current_point()
print(f"[2] 起点（各轴当前位置）：{start}")
for ax in dev.axes:
    pos = ax.get()
    assert ax.low <= pos <= ax.high, f"{ax.name} 当前位置 {pos} 不在量程 [{ax.low},{ax.high}] 内"
    probe = pos + 0.02 * (ax.high - ax.low)          # 小幅试探，确认能动且能读回
    probe = min(probe, ax.high)
    ax.move(probe)
    back = ax.get()
    print(f"    {ax.name}: move({probe:.4f}) → get()={back:.4f}  误差 {abs(back-probe):.2e}")
    ax.move(pos)                                     # 归位
print("[3] 轴 move/get 往返完成（误差应在你的定位精度量级；若恒为 0 请确认 get() 不是回读目标值）")

# --- 测量：逐通道读一次 ---
ev = dev.evaluator()
y = ev.evaluate(start)
print(f"[4] 全通道读一次：{y}")

# --- 选择性读取：只要第一个 y，其它通道不应被读 ---
first = list(y)[0]
ev2 = dev.evaluator()
y2 = ev2.evaluate(start, channels=[first])
assert set(y2) == {first}, f"选择性读取失败，返回了 {set(y2)}"
print(f"[5] 选择性读取 OK：只要 {first} 时，其它通道未被读取")

# --- 平均：averages>1 应产生多次真实采集（有噪声时读数应有起伏）---
ev3 = dev.evaluator(averages=4)
y3 = ev3.evaluate(start)
print(f"[6] averages=4 读数：{y3}；累计测量耗时 {ev3.sim_seconds:.2f}s")
print(f"    单次耗时 {ev.sim_seconds:.2f}s → 4 次应约为 4 倍；若相等说明 cost 未填或缓存有问题")

# --- Source：确认一次采集出多通道时仪器只触发一次 ---
srcs = {id(s): s for m in dev.meters if (s := getattr(m, "source", None)) is not None}
if srcs:
    print(f"[7] 共享采集 Source：{[s.name for s in srcs.values()]}；"
          f"其通道分组 {[(m.name, m.group) for m in dev.meters if getattr(m,'source',None)]}")
    print("    → 用计数器包一层 read_fn 可确认：一步要同源的 2 个 y 时只应触发 1 次")
else:
    print("[7] 未使用 Source；若你的仪器一次读取能出多个 y，请改用 Source")

print("\n冒烟通过。接下来：python run_device.py 你的设备.py")
```

对内置模板跑一遍确认脚本本身没问题：

```bash
python smoke_device.py examples/device_template.py
```

---

## 2. 症状 → 原因对照

| 症状 | 多半是这个原因 |
|---|---|
| 优化从量程中点开始，不是从台架当前位置 | `Axis.get()` 返回的是初始化默认值，没读真实位置；或调用方显式传了 `start_point` |
| 扫描一上来就跑到量程两端 | 用了绝对扫描。真实台架要设 `span_frac>0`（0.2~0.6）做相对窗口；注意 `span_frac=1` 起点在中点时≈全程 |
| `averages` 调大后噪声没变小、耗时也没涨 | 设备文件里自己缓存了读数（或仪器内部保持模式）→ N 次平均取到同一个数。缓存只能交给 `Source` |
| 两个 y 明明该同时测，判据却时对时错 | 各写了 `read_fn` → 来自不同次采集。改用 `Source` 让同一次采集分发多通道 |
| 只优化 y1 的步骤仍然很慢 | 条件表达式里引用了 y2（`keep`/`stop`/`until`），它就进了必读通道集合；或用了 `HardwareEvaluator`（每步全读） |
| 画布预估耗时明显偏离实测 | `cost` 没填或填错；并行测的通道没设成同一 `group` |
| 读数偶发跳变/与位置对不上 | `move()` 没阻塞到位，且 `settle_time=0`。补 `settle_time` 或让 move 阻塞 |
| 报 `KeyError: 'powerX'` | `Source.read_fn` 某次返回的 dict 缺了已声明的通道键 |
| 报 `meter xx: provide read_fn or override get()` | `Meter` 既没给 `read_fn`、也没派生自 `Source`、也没重写 `get()` |
| 报 `device must define at least one axis (x) and one meter (y)` | 文件没导出 `AXES`/`METERS`，或 `build()` 返回值不是 `(axes, meters)` |
| 轴动了但目标值不变 | 轴名与实际驱动的物理轴对不上；或 `move()` 里把相对位移当成了绝对位置 |
| 画布 2D 视图没有等高线 | 真实设备无解析响应面，`/surface` 返回 400 —— 正常，轨迹图仍可用 |

---

## 3. 上台架前的最后确认

- [ ] 安全限位（`SafetyLimits`）已按真实机械行程设好，且**比软件量程更紧或相等**
- [ ] `move()` 的单位与 `low/high/resolution` 一致
- [ ] 急停/断连时 `move`、`read_fn` 会抛异常（而不是静默返回旧值）——异常会中止运行，比带着错数据继续优化安全
- [ ] `averages` 与 `settle_time` 按仪器实际稳定时间设过一次
- [ ] 冒烟脚本 7 项全过
