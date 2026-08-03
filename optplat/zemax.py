"""Zemax OpticStudio backend — an Evaluator backed by the OpticStudio MCP server.

    x  (platform variables)  --write-->  Lens Data Editor    (Coordinate Break
                                                              PARM / thickness…)
    y  (platform objectives) <--read---  Merit Function Editor

The whole platform only ever sees the Evaluator contract (`evaluate(x) -> dict`),
exactly like `HardwareEvaluator`. So a graph/pipeline written against a real
motor stage runs unchanged against Zemax, and vice versa — that is the point:
simulation and hardware are two backends of the same IR, and can be mixed
(see `CompositeEvaluator`).

Talks to https://github.com/zym1998year/OpticStudioMCPServer (MIT) over MCP
stdio. That server is a separate process (it needs Windows + the ZOS-API), so
nothing about it is linked into this codebase.

Mapping model
-------------
`ZemaxKnob` says where one platform variable physically lands:

    ZemaxKnob(surface=3, param="decenter_x")          # CB surface 3, PARM 1
    ZemaxKnob(surface=3, kind="thickness")            # surface 3 thickness

Coordinate Break parameter numbers (fixed by OpticStudio):
    1 Decenter X   2 Decenter Y   3 Tilt About X
    4 Tilt About Y 5 Tilt About Z 6 Order (0 = decenter-then-tilt, 1 = reverse)

Followers ("跟随"): the classic tilt/decenter-and-return pair — a second
Coordinate Break that must mirror the first (scale = -1). Two ways to enforce it:

  * mode="write"  (default) — the platform writes the follower's value on every
    evaluation. Works on any OpticStudio version, and the value is visible in
    the platform's own history.
  * mode="pickup" — install an OpticStudio Pickup solve once at bind time and
    let Zemax maintain it. Fewer writes per evaluation, but the pickup column
    numbering is OpticStudio-build specific, so pass `pickup_column` explicitly
    if the default is wrong for your build.

Objectives come from the Merit Function Editor: the total merit value, and/or
individual operand rows (by row number, or by first row of a given operand type
such as "RSCE"/"EFFL").
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field

from .mcp_client import McpStdioClient
from .vocs import VOCS, Objective, ObjectiveMode, Variable

# Coordinate Break PARM numbers, per the OpticStudio surface definition.
CB_PARAMS: dict[str, int] = {
    "decenter_x": 1,
    "decenter_y": 2,
    "tilt_x": 3,
    "tilt_y": 4,
    "tilt_z": 5,
    "order": 6,
}

# Non-parameter lens-data columns, as named by zemax_set_surface.
SURFACE_FIELDS = {"thickness", "radius", "conic", "semi_diameter"}
_SET_SURFACE_ARG = {
    "thickness": "thickness",
    "radius": "radius",
    "conic": "conic",
    "semi_diameter": "semiDiameter",
}

Kind = Literal["param", "thickness", "radius", "conic", "semi_diameter"]


def param_number(param: Union[int, str]) -> int:
    """Resolve 'decenter_x' / 'tilt_z' / 3 to a PARM number."""
    if isinstance(param, int):
        return param
    key = str(param).strip().lower().replace(" ", "_")
    if key in CB_PARAMS:
        return CB_PARAMS[key]
    if key.startswith("param"):
        return int(key[5:])
    raise ValueError(f"unknown Coordinate Break parameter: {param!r}. "
                     f"Use a number or one of {sorted(CB_PARAMS)}")


class Follower(BaseModel):
    """A surface that must track a master knob (e.g. the return Coordinate Break).

    zemax_value(follower) = scale * zemax_value(master) + offset
    """
    surface: int
    kind: Kind = "param"
    param: Optional[Union[int, str]] = None      # default: same as the master
    scale: float = -1.0                          # -1 = tilt/decenter-and-return
    offset: float = 0.0
    mode: Literal["write", "pickup"] = "write"
    pickup_column: Optional[int] = None          # only for mode="pickup"


class ZemaxKnob(BaseModel):
    """Where one platform variable lands in the Lens Data Editor.

    zemax_value = scale * x + offset, so platform units need not match Zemax's
    (e.g. drive microns while the LDE is in millimetres: scale=0.001).
    """
    surface: int
    kind: Kind = "param"
    param: Optional[Union[int, str]] = None      # required when kind == "param"
    scale: float = 1.0
    offset: float = 0.0
    followers: list[Follower] = Field(default_factory=list)
    # optional, only so a VOCS can be generated straight from the binding
    low: Optional[float] = None
    high: Optional[float] = None

    def to_zemax(self, x: float) -> float:
        return self.scale * x + self.offset

    def resolved_param(self) -> int:
        if self.kind != "param":
            raise ValueError(f"knob on surface {self.surface} is kind={self.kind}, not a PARM")
        if self.param is None:
            raise ValueError(f"knob on surface {self.surface} needs `param`")
        return param_number(self.param)


class OperandRef(BaseModel):
    """Which Merit Function Editor row feeds one platform objective."""
    row: Optional[int] = None                    # 1-indexed MFE row
    type: Optional[str] = None                   # or: first row of this operand type
    field: Literal["value", "contribution", "target", "weight"] = "value"

    def matches(self, op: dict, index: int) -> bool:
        if self.row is not None:
            return int(op.get("row", op.get("Row", index))) == self.row
        if self.type is not None:
            return str(op.get("type", op.get("Type", ""))).upper() == self.type.upper()
        return False


class MeritSpec(BaseModel):
    """How to turn one Merit Function Editor read into the platform's y dict."""
    total: Optional[str] = "merit"                          # y key for total merit
    operands: dict[str, OperandRef] = Field(default_factory=dict)
    include_values: bool = True                             # recalculate on read


class ZemaxConnection(BaseModel):
    """How to reach the OpticStudio MCP server."""
    command: list[str]                            # e.g. ["C:/…/OpticStudioMCPServer.exe"]
    mode: Literal["standalone", "extension"] = "extension"
    instance: int = 0                             # extension-mode instance id
    file: Optional[str] = None                    # .zmx / .zos to open on bind
    timeout: float = 120.0
    cwd: Optional[str] = None
    env: Optional[dict[str, str]] = None
    connect_on_bind: bool = True                  # server pre-connects standalone


class ZemaxBinding(BaseModel):
    """The complete, declarative x/y mapping — part of the IR, canvas-editable."""
    knobs: dict[str, ZemaxKnob] = Field(default_factory=dict)
    merit: MeritSpec = Field(default_factory=MeritSpec)

    def build_vocs(self, objectives: Optional[dict[str, Objective]] = None) -> VOCS:
        """Derive a VOCS from the binding (knobs with low/high + merit objectives)."""
        variables = {
            name: Variable(low=k.low, high=k.high)
            for name, k in self.knobs.items() if k.low is not None and k.high is not None
        }
        if objectives is None:
            objectives = {}
            if self.merit.total:
                # merit function is a cost -> minimise
                objectives[self.merit.total] = Objective(mode=ObjectiveMode.MINIMIZE)
            for name in self.merit.operands:
                objectives[name] = Objective(mode=ObjectiveMode.MINIMIZE)
        return VOCS(variables=variables, objectives=objectives)


class ZemaxEvaluator:
    """Evaluator whose 'instrument' is OpticStudio, driven over MCP.

    Same duck type as `Evaluator` / `HardwareEvaluator`: evaluate(x, stage)->dict.
    `safety` (a `hardware.SafetyLimits`) is honoured here too — a raytrace cannot
    crash a stage, but the same binding is meant to be re-pointed at real
    hardware, so the guardrail stays in the same place in both backends.
    """

    def __init__(self,
                 binding: ZemaxBinding,
                 connection: ZemaxConnection,
                 client: Optional[Any] = None,
                 safety: Optional[Any] = None,
                 store: Optional[Any] = None):
        self.binding = binding
        self.connection = connection
        self.safety = safety
        self.store = store
        self.history: list[dict] = []
        self._client = client                 # injectable (tests / shared session)
        self._owns_client = client is None
        self._bound = False

    # ---- session ------------------------------------------------------------
    @property
    def client(self):
        if self._client is None:
            c = self.connection
            self._client = McpStdioClient(c.command, env=c.env, cwd=c.cwd,
                                          timeout=c.timeout).start()
        return self._client

    def bind(self) -> "ZemaxEvaluator":
        """Connect, open the design, install pickup solves. Idempotent."""
        if self._bound:
            return self
        c = self.connection
        if c.connect_on_bind:
            self.client.call_tool("zemax_connect",
                                  {"mode": c.mode, "instanceNumber": c.instance})
        if c.file:
            self.client.call_tool("zemax_open_file", {"filePath": c.file})
        self._install_pickups()
        self._bound = True
        return self

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None
        self._bound = False

    def _install_pickups(self) -> None:
        for name, knob in self.binding.knobs.items():
            for f in knob.followers:
                if f.mode != "pickup":
                    continue
                if f.kind != "param" or knob.kind != "param":
                    raise ValueError(f"pickup follower for '{name}' is only supported "
                                     f"between PARM cells; use mode='write'")
                master_param = knob.resolved_param()
                follower_param = param_number(f.param) if f.param is not None else master_param
                self.client.call_tool("zemax_set_surface_solve", {
                    "surfaceNumber": f.surface,
                    "property": f"param{follower_param}",
                    "solveType": "Pickup",
                    "pickupSurface": knob.surface,
                    "pickupColumn": f.pickup_column if f.pickup_column is not None
                                    else master_param,
                    "scaleFactor": f.scale,
                    "offset": f.offset,
                })

    # ---- write / read -------------------------------------------------------
    def _writes(self, x: dict[str, float]) -> tuple[dict[int, dict[int, float]],
                                                    dict[int, dict[str, float]]]:
        """Collapse x (+ its write-mode followers) into per-surface write batches."""
        params: dict[int, dict[int, float]] = {}     # surface -> {parm: value}
        fields: dict[int, dict[str, float]] = {}     # surface -> {arg: value}

        def put(surface: int, kind: str, param: Optional[Union[int, str]], value: float):
            if kind == "param":
                params.setdefault(surface, {})[param_number(param)] = value
            else:
                fields.setdefault(surface, {})[_SET_SURFACE_ARG[kind]] = value

        for name, knob in self.binding.knobs.items():
            if name not in x:
                continue
            value = knob.to_zemax(x[name])
            put(knob.surface, knob.kind, knob.param, value)
            for f in knob.followers:
                if f.mode != "write":
                    continue
                put(f.surface, f.kind,
                    f.param if f.param is not None else knob.param,
                    f.scale * value + f.offset)
        return params, fields

    def _apply(self, x: dict[str, float]) -> None:
        params, fields = self._writes(x)
        for surface, parms in params.items():
            # one call per surface: batchSet is 'num:value,num:value'
            batch = ",".join(f"{n}:{v!r}" for n, v in sorted(parms.items()))
            res = self.client.call_tool("zemax_set_surface_parameter",
                                        {"surfaceNumber": surface, "batchSet": batch})
            _check(res, f"set surface {surface} params {batch}")
        for surface, args in fields.items():
            res = self.client.call_tool("zemax_set_surface",
                                        {"surfaceNumber": surface, **args})
            _check(res, f"set surface {surface} {args}")

    def _read(self) -> dict[str, float]:
        spec = self.binding.merit
        res = self.client.call_tool("zemax_get_merit_function",
                                    {"includeValues": spec.include_values})
        _check(res, "get merit function")
        y: dict[str, float] = {}
        if spec.total:
            y[spec.total] = float(_get(res, "totalMerit", 0.0))
        if spec.operands:
            operands = _get(res, "operands", []) or []
            for name, ref in spec.operands.items():
                op = next((o for i, o in enumerate(operands, start=1) if ref.matches(o, i)), None)
                if op is None:
                    raise KeyError(f"objective '{name}': no merit-function operand matches "
                                   f"{ref.model_dump(exclude_none=True)}")
                y[name] = float(_get(op, ref.field, 0.0))
        return y

    # ---- Evaluator contract -------------------------------------------------
    def evaluate(self, x: dict[str, float], stage: str = "") -> dict[str, float]:
        self.bind()
        target = self.safety.enforce(x) if self.safety else x
        self._apply(target)
        y = self._read()
        self.history.append({"stage": stage, **target, **y})
        if self.store is not None:
            self.store.append(stage, target, y)
        return y


class CompositeEvaluator:
    """Fan one evaluation out to several backends and merge the results.

    The compatibility story: a step can drive Zemax knobs and real stage axes in
    the same evaluation (model-in-the-loop calibration), or read a power meter
    while a simulated design supplies a reference. Each child gets only the
    variables it owns; y keys are merged (optionally prefixed to avoid clashes).

        CompositeEvaluator({"sim": (zemax_eval, ["cb_tx", "cb_ty"]),
                            "rig": (hw_eval,    ["x", "y"])})
    """

    def __init__(self,
                 children: dict[str, tuple[Any, list[str]]],
                 prefix_outputs: bool = False,
                 store: Optional[Any] = None):
        self.children = children
        self.prefix_outputs = prefix_outputs
        self.store = store
        self.history: list[dict] = []

    def evaluate(self, x: dict[str, float], stage: str = "") -> dict[str, float]:
        y: dict[str, float] = {}
        for name, (child, owned) in self.children.items():
            sub = {k: v for k, v in x.items() if k in owned}
            out = child.evaluate(sub, stage=stage)
            for k, v in out.items():
                y[f"{name}.{k}" if self.prefix_outputs else k] = v
        self.history.append({"stage": stage, **x, **y})
        if self.store is not None:
            self.store.append(stage, x, y)
        return y


# ---- result helpers ---------------------------------------------------------
def _get(obj: dict, key: str, default=None):
    """Tolerate camelCase / PascalCase / snake_case keys across server versions."""
    if not isinstance(obj, dict):
        return default
    variants = {key, key[0].upper() + key[1:], key[0].lower() + key[1:]}
    snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in key).lstrip("_")
    variants |= {snake, snake.replace("_", "")}
    lowered = {k.lower().replace("_", ""): v for k, v in obj.items()}
    for v in variants:
        if v in obj:
            return obj[v]
    return lowered.get(key.lower().replace("_", ""), default)


def _check(res: Any, what: str) -> None:
    if isinstance(res, dict) and _get(res, "success") is False:
        raise RuntimeError(f"OpticStudio {what} failed: {_get(res, 'error')}")
