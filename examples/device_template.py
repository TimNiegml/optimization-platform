"""外部设备定义模板 —— 复制这个文件，把里面的 move/get 换成你真实仪器的调用即可。

平台通过一个极小的接口驱动优化，你只需定义：
  * 每个自变量 x = 一个『轴 Axis』：能 move(到某位置) + get(读当前位置)
  * 每个因变量 y = 一个『测量 Meter』：能 get(读一次测量值)

平台会**自动读出有几个 x、几个 y**（就是下面 AXES / METERS 的长度），据此建好
VOCS、把优化算法、安全限位、测量耗时、画布、Agent 全部套上——你不用改平台。

两种用法：
  1) 命令行直接跑（完全不依赖后端）：
        python run_device.py examples/device_template.py
  2) 让后端/画布用你的设备（画布会自动显示你的 x/y 个数）：
        OPTPLAT_DEVICE=examples/device_template.py python -m optplat.api

真实硬件：把 Axis.move/get 换成电机台的移动/读编码器（如 PyVISA/PyMeasure），
把 Meter.get 换成读功率计/偏振分析仪。**注意**：真实台架没有绝对坐标，优化的
『起点』默认取 Axis.get() 的当前位置（见 DeviceSpec.current_point），相对扫描
（网格扫描的 span_frac>0）就会围绕当前位置进行。
"""
import math

from optplat.userdev import Axis, Meter, Source

# ---- 1) 定义轴（自变量 x）。真实台架里 move/get 换成电机调用 ----
# 这里用带内部状态的模拟轴：move 记录位置，get 读回位置。
x1 = Axis("x1", low=-2.0, high=6.0, pos=5.0,
          device="六轴位移台", param="axis_1")     # pos = 上电时的当前位置（起点从这来）
x2 = Axis("x2", low=-5.0, high=3.0, pos=-3.0,
          device="六轴位移台", param="axis_2")

AXES = [x1, x2]


# ---- 2) 定义测量（因变量 y）。真实台架里 get() 换成读仪器 ----
# 模拟物理：y1=耦合功率（高斯主瓣，峰在 (2,-1)）；y2=某均衡量，越接近 (2,-1) 越好。
def _read_power():
    p1, p2 = x1.get(), x2.get()
    return math.exp(-((p1 - 2.0) ** 2 + (p2 + 1.0) ** 2) / (2 * 1.2 ** 2))


def _read_balance():
    p1, p2 = x1.get(), x2.get()
    # 举例：一个想调到 0 的失配量（用 mode="target", target=0）
    return 0.3 * (p1 - 2.0) - 0.2 * (p2 + 1.0)


# 每个 Meter 可选标注：mode(maximize/minimize/target)、target、cost(读一次耗时秒)、
# group(同组并行测/不同组串行测)、device/param(通道映射)。平台按这些建 VOCS + 计时。
METERS = [
    Meter("y1", _read_power, mode="maximize", cost=1.0, group="opt",
          device="光功率计", param="power"),
    Meter("y2", _read_balance, mode="target", target=0.0, cost=2.0, group="opt",
          device="偏振分析仪", param="balance"),
]


# ---- 3) 一次读取拿到多个通道？用 Source（共享一次采集）----
# 有的仪器一次触发就返回一整组读数（双通道功率计 read() → power1 + power2），
# 如果给每个 y 单独写 read_fn，仪器会被触发两次（慢，且两个 y 来自不同次采集）。
# Source 读一次、缓存，派生出来的 meter 都吃这次采集；平台在**每轮平均前**清缓存，
# 所以 averages=8 仍然是 8 次真实采集、8 个独立噪声样本，只是不会再乘以通道数。
# 同一 Source 的通道默认同组（并行计时），采集时间只算一次而不是逐通道相加。
#
# def _read_pm_both():
#     p1, p2 = inst.read_both()          # 一次触发，返回两个功率
#     return {"power1": p1, "power2": p2}
#
# pm = Source("pm", _read_pm_both, cost=1.0, device="双通道光功率计")
# METERS = [
#     pm.meter("y1", "power1", mode="maximize"),     # y1 取 power1
#     pm.meter("y3", "power2", mode="target", target=0.0),   # y3 取 power2
#     Meter("y2", _read_balance, mode="target", target=0.0, cost=2.0),  # 另一台仪器照旧
# ]

# 同一次采集返回矩阵、向量和标量时，写法完全相同：
# def _read_camera_once():
#     result = camera.measure()
#     return {"image": result.image, "spectrum": result.spectrum, "power": result.power}
#
# camera_source = Source("camera", _read_camera_once, cost=1.0, device="相机/光谱仪")
# METERS = [
#     camera_source.meter("y1", "image", value_type="matrix", mode="scan",
#                         display_name="MTF矩阵", unit="dB", shape=(32, 101), dtype="float64"),
#     camera_source.meter("y2", "spectrum", value_type="vector", mode="scan",
#                         display_name="MTF曲线", unit="dB", shape=(101,), dtype="float64"),
#     camera_source.meter("y3", "power", value_type="scalar", mode="maximize",
#                         display_name="耦合功率", unit="dBm", dtype="float64"),
# ]


# 可选：也可以用 build() 返回 (AXES, METERS)，适合需要初始化/连接仪器的场景：
# def build():
#     stage = connect_stage(); meter = connect_meter()
#     axes = [Axis("x1", -2, 6, pos=stage.pos("A")), ...]
#     meters = [Meter("y1", lambda: meter.read_power()), ...]
#     return axes, meters
