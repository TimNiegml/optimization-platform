"""Algorithm / node registry — the plug-in contract.

Every algorithm node declares a small, machine-readable interface:
  * which params it exposes (type + default + range/options)  -> the canvas
    auto-builds the node's config panel from this
  * whether it is single-variable
  * a builder(vocs, step) -> Generator  (ask/tell)

Built-ins are registered below. A CUSTOM algorithm plugs in with:

    from optplat.registry import register_algorithm, AlgorithmSpec
    class MyGen(Generator): ...            # implement ask()/tell()/done/best_x
    register_algorithm(AlgorithmSpec(
        name="my_algo", category="custom", single_var=False,
        params={"gain": {"type": "float", "default": 1.0}},
        builder=lambda vocs, step: MyGen(vocs, step["variables"], step["objective"],
                                         gain=step.get("gain", 1.0)),
    ))

...and it immediately appears as a draggable node and runs in any graph/pipeline.
The engine never hard-codes algorithm names — it only calls build_generator().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .generators import (
    CoordinateDescent,
    DampedSensitivity,
    FormulaMethod,
    Generator,
    GradientAscent,
    GridScan,
    NelderMead,
    MatrixPolicy,
    ParametricFit,
    SensitivityScan,
    SpiralScan,
    SurrogateFit,
)
from .vocs import VOCS


@dataclass
class AlgorithmSpec:
    name: str
    category: str
    single_var: bool
    builder: Callable[[VOCS, dict], Generator]
    params: dict = field(default_factory=dict)   # param -> {type, default, ...}
    label: str = ""       # human-facing (Chinese) name; falls back to `name`
    desc: str = ""        # one-line 说明, shown as a canvas tooltip
    # 画布左栏的分组（人怎么找算法）：找光 → 粗调 → 精调 → 求解/表征。
    # 与 `category`（自动调优按它归相）解耦：一个是给人看的货架，一个是给搜索用的相。
    group: str = "fine"


# 节点货架分组：找光 → 粗调 → 精调 → 求解/表征（画布左栏按此排列）
GROUPS = [
    ("find", "① 找光", "从当前位置往外找到第一束光/有效信号，配 stop.target 扫到阈值即停"),
    ("coarse", "② 粗调", "已经有信号，快速逼近到峰附近（全局或大步长）"),
    ("fine", "③ 精调", "在峰附近收敛到最优，或用拟合直接定峰"),
    ("solve", "④ 求解 / 表征", "多进多出定值、灵敏度测量——不是寻优"),
]
GROUP_LABEL = {k: lab for k, lab, _ in GROUPS}

REGISTRY: dict[str, AlgorithmSpec] = {}


def register_algorithm(spec: AlgorithmSpec) -> None:
    REGISTRY[spec.name] = spec


def build_generator(vocs: VOCS, step: dict) -> Generator:
    algo = step["algorithm"]
    if algo not in REGISTRY:
        raise ValueError(f"unknown algorithm: {algo} (registered: {sorted(REGISTRY)})")
    return REGISTRY[algo].builder(vocs, step)


def algorithm_catalog() -> list[dict]:
    """Everything the canvas needs to render the node palette + config panels."""
    return [
        {"name": s.name, "label": s.label or s.name, "desc": s.desc,
         "category": s.category, "group": s.group,
         "group_label": GROUP_LABEL.get(s.group, s.group),
         "single_var": s.single_var, "params": s.params}
        for s in REGISTRY.values()
    ]


# ---------------- built-in algorithm nodes ----------------
def _bayes_builder(vocs, step):
    from .bayes import BayesianGenerator
    return BayesianGenerator(vocs, step["variables"], step["objective"],
                             sampler=step.get("sampler", "tpe"),
                             n_calls=step.get("n_calls", 40), seed=step.get("seed"),
                             span_frac=step.get("span_frac", 0.0),
                             n_startup=step.get("n_startup", 10),
                             explore=step.get("explore", 0.1))


_BUILTINS = [
    AlgorithmSpec(
        "grid_scan", "find-light", False,
        lambda v, s: GridScan(v, s["variables"], s["objective"],
                              n_per_axis=s.get("n_per_axis", 7),
                              span_frac=s.get("span_frac", 0.0)),
        {"n_per_axis": {"type": "int", "default": 7, "min": 3, "max": 21,
                        "label": "每轴取点数"},
         "span_frac": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0,
                       "label": "扫描窗口(相对起点)",
                       "hint": "0=全程绝对扫描；>0=以起点为中心、该比例的相对窗口(无绝对坐标)"}},
        group="find",
        label="网格扫描", desc="第一阶段找光：在选定变量上打网格粗扫，快速定位大致位置。",
    ),
    AlgorithmSpec(
        "line_scan", "find-light", False,
        lambda v, s: GridScan(v, s["variables"], s["objective"],
                              n_per_axis=s.get("n_per_axis", 9),
                              span_frac=s.get("span_frac", 0.0)),
        {"n_per_axis": {"type": "int", "default": 9, "min": 3, "max": 51,
                        "label": "取点数"},
         "span_frac": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0,
                       "label": "扫描窗口(相对起点)",
                       "hint": "0=全程绝对扫描；>0=以起点为中心、该比例的相对窗口(无绝对坐标)"}},
        group="find",
        label="线扫描", desc="沿单/少数轴逐点扫描，产特性曲线或找过阈值的第一束光。",
    ),
    AlgorithmSpec(
        "spiral_scan", "find-light", False,
        lambda v, s: SpiralScan(v, s["variables"], s["objective"],
                                step=s.get("step", 0.1), steps=s.get("steps"),
                                points_per_turn=s.get("points_per_turn", 12),
                                turns=s.get("turns", 4)),
        {"step": {"type": "float", "default": 0.1, "min": 1e-6, "max": 1e6,
                  "label": "每圈半径增量(绝对)", "hint": "转满一圈后半径增加这么多；各轴可在下表单独设"},
         "steps": {"type": "axis_steps", "default": {}, "label": "每轴半径增量(可空)",
                   "hint": "不同执行器量纲/行程不同时逐轴设；留空=用上面的统一值"},
         "points_per_turn": {"type": "int", "default": 12, "min": 3, "max": 60,
                             "label": "每圈点数"},
         "turns": {"type": "int", "default": 4, "min": 1, "max": 50, "label": "圈数"}},
        group="find",
        label="螺旋扫描",
        desc="从当前位置(原点)向外螺旋展开找光：先密搜近处再逐圈扩大，配 stop.target 扫到阈值即停；"
             "1 维退化为两侧交替外扩，2 维为阿基米德螺旋，≥3 维为球面低差异方向的向外扩散。",
    ),
    AlgorithmSpec(
        "coordinate_descent", "local", False,
        lambda v, s: CoordinateDescent(v, s["variables"], s["objective"],
                                       init_step_frac=s.get("step_frac", 0.25),
                                       steps=s.get("steps")),
        {"step_frac": {"type": "float", "default": 0.25, "min": 0.01, "max": 1.0,
                       "label": "初始步距比例", "hint": "各轴步距=该比例×轴量程(可被下方每轴步距覆盖)"},
         "steps": {"type": "axis_steps", "default": {}, "label": "每轴步距(绝对,可空)",
                   "hint": "给某轴单独设绝对步距，留空=用上面的比例"}},
        group="fine",
        label="坐标下降", desc="逐个坐标轴交替精调，稳健的本地精调算法；每轴步距可单独设。",
    ),
    AlgorithmSpec(
        "nelder_mead", "local", False,
        lambda v, s: NelderMead(v, s["variables"], s["objective"],
                                init_step_frac=s.get("init_step_frac", 0.1),
                                init_steps=s.get("init_steps")),
        {"init_step_frac": {"type": "float", "default": 0.1, "min": 0.01, "max": 1.0,
                            "label": "初始单纯形比例", "hint": "各轴 simplex 边长=该比例×轴量程(可被下方每轴覆盖)"},
         "init_steps": {"type": "axis_steps", "default": {}, "label": "每轴初始 simplex(绝对,可空)",
                        "hint": "给某轴单独设初始 simplex 边长，留空=用上面的比例"}},
        group="fine",
        label="单纯形法", desc="Nelder-Mead 无导数本地优化，适合多变量峰值精调；初始 simplex 每轴可单独设。",
    ),
    AlgorithmSpec(
        "gradient_ascent", "local", False,
        lambda v, s: GradientAscent(v, s["variables"], s["objective"],
                                    probe_frac=s.get("probe_frac", 0.02),
                                    step_frac=s.get("step_frac", 0.15)),
        {"step_frac": {"type": "float", "default": 0.15, "min": 0.01, "max": 0.5,
                       "label": "步长比例"},
         "probe_frac": {"type": "float", "default": 0.02, "min": 0.005, "max": 0.1,
                        "label": "探测步比例"}},
        group="coarse",
        label="梯度上升(PI闪电式)",
        desc="有限差分测局部梯度、沿上升方向步进+步长自适应，模拟 PI 闪电式快速对准。",
    ),
    AlgorithmSpec(
        "quadratic_fit", "fit", True,
        lambda v, s: SurrogateFit(v, s["variables"], s["objective"], model="quadratic",
                                  n_samples=s.get("n_samples", 5), r2_gate=s.get("r2_gate", 0.9),
                                  span_frac=s.get("span_frac", 0.0)),
        {"n_samples": {"type": "int", "default": 5, "min": 3, "max": 15, "label": "采样点数"},
         "r2_gate": {"type": "float", "default": 0.9, "min": 0.0, "max": 0.99,
                     "label": "R² 守门阈值"},
         "span_frac": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0,
                       "label": "采样窗口(相对起点)", "hint": "0=全程绝对；>0=以起点为中心的相对窗口"}},
        group="fine",
        label="二次拟合", desc="采几点拟合抛物线定峰；R² 不达标自动回退，防外推跑飞。",
    ),
    AlgorithmSpec(
        "gaussian_fit", "fit", True,
        lambda v, s: SurrogateFit(v, s["variables"], s["objective"], model="gaussian",
                                  n_samples=s.get("n_samples", 5), r2_gate=s.get("r2_gate", 0.9),
                                  span_frac=s.get("span_frac", 0.0)),
        {"n_samples": {"type": "int", "default": 5, "min": 3, "max": 15, "label": "采样点数"},
         "r2_gate": {"type": "float", "default": 0.9, "min": 0.0, "max": 0.99,
                     "label": "R² 守门阈值"},
         "span_frac": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0,
                       "label": "采样窗口(相对起点)", "hint": "0=全程绝对；>0=以起点为中心的相对窗口"}},
        group="fine",
        label="高斯拟合", desc="用高斯峰型拟合定峰，适合耦合功率这类钟形曲线。",
    ),
    AlgorithmSpec(
        "parametric_fit", "fit", True,
        lambda v, s: ParametricFit(v, s["variables"], s["objective"],
                                   model=s.get("model", "quadratic"), fixed=s.get("fixed"),
                                   hints=s.get("hints"), n_samples=s.get("n_samples", 5),
                                   r2_gate=s.get("r2_gate", 0.9), span_frac=s.get("span_frac", 0.0)),
        {"model": {"type": "str", "default": "quadratic",
                   "hint": "gaussian / quadratic / 自定义表达式", "label": "模型"},
         "fixed": {"type": "dict", "default": {}, "hint": "钉死的已知参数", "label": "钉死参数"},
         "n_samples": {"type": "int", "default": 5, "min": 3, "max": 15, "label": "采样点数"},
         "span_frac": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0,
                       "label": "采样窗口(相对起点)", "hint": "0=全程绝对；>0=以起点为中心的相对窗口"}},
        group="fine",
        label="参数拟合(非标)", desc="非标拟合：钉死已知参数、只解自由参数，支持自定义模型表达式。",
    ),
    AlgorithmSpec(
        "formula", "analytic", True,
        lambda v, s: FormulaMethod(v, s["variables"], s["objective"],
                                   span_frac=s.get("span_frac", 0.5)),
        {"span_frac": {"type": "float", "default": 0.5, "min": 0.1, "max": 1.0,
                       "label": "采样跨度比例"}},
        group="fine",
        label="公式法(解析)", desc="三点解析定峰，参数拟合的快速特例，几个点直接算出极值位置。",
    ),
    AlgorithmSpec(
        "matrix_policy", "solve", False,
        lambda v, s: MatrixPolicy(
            v, s["variables"], s["objective"], input_channel=s.get("input_channel", ""),
            output_map=s.get("output_map"), weights=s.get("weights"), bias=s.get("bias"),
            feature_mode=s.get("feature_mode", "flatten"),
            action_mode=s.get("action_mode", "absolute"), gain=s.get("gain", 1.0),
            max_actions=s.get("max_actions", 1)),
        {"input_channel": {"type": "str", "default": "", "label": "矩阵输入通道"},
         "feature_mode": {"type": "enum", "default": "flatten",
                          "options": ["flatten", "row_mean", "column_mean"], "label": "矩阵特征"},
         "weights": {"type": "matrix", "default": [], "label": "示例模型权重 W",
                     "hint": "二维数组 [输出数, 特征数]；留空=特征向量直通"},
         "bias": {"type": "list", "default": [], "label": "示例模型偏置 b"},
         "output_map": {"type": "output_map", "default": {}, "label": "输出→自变量映射",
                        "hint": "每个 x 选择模型输出向量的下标"},
         "action_mode": {"type": "enum", "default": "absolute",
                         "options": ["absolute", "delta"], "label": "输出方式"},
         "gain": {"type": "float", "default": 1.0, "label": "输出增益"},
         "max_actions": {"type": "int", "default": 1, "min": 1, "max": 100, "label": "推理次数"}},
        group="solve", label="矩阵模型控制器",
        desc="矩阵测量→特征→模型输出向量→按下标分配到 x1/x2…；内置仿射模型示例，可替换为深度学习推理。",
    ),
    AlgorithmSpec(
        "damped_sensitivity", "solve", False,
        lambda v, s: DampedSensitivity(
            v, s["variables"], s["objective"],
            sensitivity=s.get("sensitivity"), targets=s.get("targets"),
            damping=s.get("damping", 0.5), reg=s.get("reg", 1e-6),
            tol=s.get("tol", 1e-4), max_solves=s.get("max_solves", 40)),
        {"damping": {"type": "float", "default": 0.5, "min": 0.05, "max": 1.0,
                     "label": "阻尼比 d"},
         "reg": {"type": "float", "default": 1e-6, "min": 0.0, "max": 1.0,
                 "label": "正则 λ", "hint": "阻尼最小奇异值，越大越稳越慢"},
         "tol": {"type": "float", "default": 1e-4, "min": 0.0, "max": 1.0,
                 "label": "残差容差"},
         "targets": {"type": "dict", "default": {}, "label": "各 y 目标值",
                     "hint": "{y1: 目标, y2: 目标, ...}"},
         "sensitivity": {"type": "matrix", "default": {}, "label": "灵敏度矩阵 ∂y/∂x",
                         "hint": "{y1:{x1:.., x2:..}, ...} 或二维数组，行=y 列=x"}},
        group="solve",
        label="阻尼灵敏度求解",
        desc="多进多出定值：已知灵敏度∂y/∂x与各y目标，用阻尼最小二乘(SVD正则,比pinv稳)解Δx，"
             "迭代把多个y同时逼到目标；x/y维度任选。",
    ),
    AlgorithmSpec(
        "sensitivity_scan", "characterize", False,
        lambda v, s: SensitivityScan(
            v, s["variables"], s["objective"], step=s.get("step", 0.1),
            steps=s.get("steps"), n_points=s.get("n_points", 5),
            fit=s.get("fit", "linear"), linear_tol=s.get("linear_tol", 0.05),
            objectives=s.get("objectives", "")),
        {"step": {"type": "float", "default": 0.1, "min": 1e-6, "max": 1e6,
                  "label": "步距(绝对)", "hint": "以原点为中心、每步走多远"},
         "steps": {"type": "axis_steps", "default": {}, "label": "每轴步距(可空)",
                   "hint": "留空=用上面的统一步距"},
         "n_points": {"type": "int", "default": 5, "min": 2, "max": 51,
                      "label": "每轴点数", "hint": "含原点，建议奇数"},
         "fit": {"type": "enum", "default": "linear", "options": ["linear", "quadratic"],
                 "label": "拟合阶次", "hint": "二次拟合时斜率取原点处导数"},
         "linear_tol": {"type": "float", "default": 0.05, "min": 0.001, "max": 0.5,
                        "label": "线性范围容差", "hint": "偏离切线 ≤ 该比例×y跨度即算线性"},
         "objectives": {"type": "str", "default": "",
                        "label": "标定哪些 y(逗号分隔)", "hint": "留空=全部因变量"}},
        group="solve",
        label="灵敏度采集",
        desc="以当前点为原点逐轴扫描，测出 ∂y/∂x 灵敏度矩阵与线性范围；采集完自动回到原点。"
             "矩阵可直接喂给阻尼灵敏度求解。多原点采集与曲线作图见画布『📐 灵敏度采集』面板。",
    ),
    AlgorithmSpec(
        "bayesian", "bayesian", False, _bayes_builder,
        {"sampler": {"type": "enum", "default": "tpe", "options": ["tpe", "gp", "random"],
                     "label": "采样器"},
         "n_calls": {"type": "int", "default": 40, "min": 10, "max": 200, "label": "评估预算"},
         "span_frac": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0,
                       "label": "初始搜索范围(相对起点)",
                       "hint": "0=在全量程里搜；>0=只在以起点为中心、该比例的窗口内搜(真实台架从当前位置附近开始)"},
         "n_startup": {"type": "int", "default": 10, "min": 1, "max": 100,
                       "label": "初始随机探索点数",
                       "hint": "先纯随机撒这么多点建模型，之后才由模型指导；点少收敛快但易被初值带偏"},
         "explore": {"type": "float", "default": 0.1, "min": 0.01, "max": 0.9,
                     "label": "探索系数 γ",
                     "hint": "TPE 里『好点』的分位比例：调大=好点集合更宽松、更倾向继续探索；调小=只围着最好的几个点挖"}},
        group="coarse",
        label="贝叶斯优化",
        desc="Optuna 全局优化(TPE/GP)，评估昂贵、多变量、有约束时优先用；可限定初始搜索范围与探索系数。",
    ),
]

for _spec in _BUILTINS:
    register_algorithm(_spec)
