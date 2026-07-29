"""真实硬件接入示例 —— 把 move/get 换成你仪器的实际调用。

与 device_template.py 的区别：模板用的是模拟轴（内部变量），这里演示**真机写法**：
继承 Axis / Meter，在 move()/get() 里调你的仪器 SDK（PyVISA / PyMeasure / 厂商 DLL）。

平台只认三个方法：
    Axis.move(value)  把该轴移动到 value
    Axis.get()        读该轴**当前位置**（优化起点由此而来——真实台架无绝对坐标）
    Meter.get()       读一次该通道的测量值

跑：
    python run_device.py examples/device_real_hardware.py
    OPTPLAT_DEVICE=examples/device_real_hardware.py python -m optplat.api
"""
from optplat.userdev import Axis, Meter

# ============================================================================
# 1) 连接仪器（按你的实际 SDK 改）
# ============================================================================
# import pyvisa
# rm = pyvisa.ResourceManager()
# stage_ctrl = rm.open_resource("GPIB0::1::INSTR")     # 位移台控制器
# power_meter = rm.open_resource("GPIB0::2::INSTR")    # 光功率计


# ============================================================================
# 2) 定义轴（自变量 x）——每个可动的自由度写一个
# ============================================================================
class StageAxis(Axis):
    """一个真实电机轴。channel = 控制器里的轴号/通道名。"""

    def __init__(self, name, channel, low, high, resolution=None):
        # low/high = 允许的运动范围（平台会在此范围内寻优，并配合安全限位夹紧）
        super().__init__(name, low=low, high=high, resolution=resolution)
        self.channel = channel

    def move(self, value: float) -> None:
        # ---- 换成你的移动指令 ----
        # stage_ctrl.write(f"MOVA {self.channel},{value:.4f}")
        # stage_ctrl.query("*OPC?")            # 等待到位（阻塞直到运动完成）
        self._pos = value                      # 模拟：记录位置

    def get(self) -> float:
        # ---- 换成你的读位置指令（读编码器/当前坐标）----
        # return float(stage_ctrl.query(f"POS? {self.channel}"))
        return self._pos                       # 模拟：读回位置


x_axis = StageAxis("x", channel=1, low=-50.0, high=50.0, resolution=0.1)   # 单位 μm
y_axis = StageAxis("y", channel=2, low=-50.0, high=50.0, resolution=0.1)
z_axis = StageAxis("z", channel=3, low=0.0, high=200.0, resolution=0.1)

AXES = [x_axis, y_axis, z_axis]        # ← 平台自动读出：3 个自变量


# ============================================================================
# 3) 定义测量（因变量 y）——每个要读的指标写一个
# ============================================================================
class PowerMeter(Meter):
    """光功率计通道。"""

    def get(self) -> float:
        # ---- 换成你的读数指令 ----
        # return float(power_meter.query("READ:POW?"))
        return 0.0                             # 模拟


class PDLMeter(Meter):
    def get(self) -> float:
        # return float(pol_analyzer.query("MEAS:PDL?"))
        return 0.0


METERS = [
    # mode: maximize(越大越好) / minimize(越小越好) / target(逼近 target 值)
    # cost:  读一次耗时(秒)，平台据此估算测量总时长
    # group: 同组=并行测(耗时取组内最大)，不同组/留空=串行测(相加)
    PowerMeter("power", mode="maximize", cost=0.5, group="opt",
               device="光功率计", param="power"),
    PDLMeter("pdl", mode="minimize", cost=0.5, group="opt",
             device="偏振分析仪", param="pdl"),
]
# ← 平台自动读出：2 个因变量


# ============================================================================
# 4)（可选）需要先连仪器再建对象时，用 build() 代替上面的模块级定义
# ============================================================================
# def build():
#     ctrl = connect_controller("192.168.1.10")
#     meter = connect_meter("GPIB0::2::INSTR")
#     axes = [StageAxis("x", 1, -50, 50), StageAxis("y", 2, -50, 50)]
#     meters = [PowerMeter("power", mode="maximize", cost=0.5)]
#     return axes, meters
