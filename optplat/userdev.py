"""External device definition — plug YOUR real (or simulated) instruments in.

The platform stays generic: it never hard-codes how many inputs/outputs you have
or how to read/move them. YOU describe your setup in a small Python module that
declares **axes** (movable inputs x) and **meters** (measured outputs y). The
platform imports it, auto-discovers how many x / y there are, builds the VOCS,
and drives optimisation purely through this tiny contract:

    axis.move(value)   # command an axis to a position
    axis.get()         # read the axis' CURRENT position  → the optimisation START
                       #   point comes from here (real stages have no absolute origin,
                       #   so we start from wherever the hardware already is)
    meter.get()        # read one measurement

That is the whole coupling surface — position get/move per x, value get per y.
Algorithms, safety limits, cost/timing, the canvas and the MCP agent all keep
working unchanged; they only ever see the VOCS + an Evaluator.

Your module exposes either:
  * module-level ``AXES`` and ``METERS`` lists, or
  * a ``build()`` function returning ``(axes, meters)``.

An axis object needs: ``name``, ``low``, ``high``, ``move(value)``, ``get()``
(optional: ``resolution``). A meter object needs: ``name``, ``get()`` (optional:
``mode`` in {maximize,minimize,target}, ``target``, ``cost`` seconds, ``group``
for parallel/serial timing, ``device``, ``param``). Duck typing — subclass the
``Axis``/``Meter`` helpers here or bring your own. See
``examples/device_template.py`` for a copy-paste starting point.

Matrix-valued instruments use ``Meter("image", read_fn, value_type="matrix")``;
the reading may be a nested list, tuple, or NumPy array. Matrix channels are for
observation/visualisation, not direct scalar optimization.

When ONE instrument acquisition yields several outputs (``pm.read()`` →
power1 + power2, one y per channel), declare a ``Source`` and derive the meters
from it (``pm.meter("y1", "power1")``): the instrument is triggered once per
acquisition however many of its channels the current step wants, and the
acquisition is billed once instead of once per channel.

Load with ``load_device("path/to/your_device.py")`` (CLI: ``run_device.py``), or
point the server at it: ``OPTPLAT_DEVICE=path python -m optplat.api`` — then the
canvas / REST / MCP all reflect your real x / y automatically.
"""
from __future__ import annotations

import importlib.util
import os
from typing import Optional

from .evaluator import json_value, read_seconds
from .hardware import SafetyLimits
from .vocs import VOCS, Objective, ObjectiveMode, ObjectiveValueType, Variable


# ---- optional helper base classes (bring your own if you prefer) ----
class Axis:
    """A movable input. Override move()/get() to talk to a real motor stage."""

    def __init__(self, name: str, low: float, high: float,
                 pos: Optional[float] = None, resolution: Optional[float] = None,
                 device: Optional[str] = None, param: Optional[str] = None):
        self.name = name
        self.low = low
        self.high = high
        self.resolution = resolution
        self.device = device
        self.param = param or name
        self._pos = pos if pos is not None else 0.5 * (low + high)

    def move(self, value: float) -> None:
        self._pos = value

    def get(self) -> float:
        return self._pos


class Meter:
    """A measured output. `read_fn` returns one reading (override get() for real HW).

    When several outputs come from ONE instrument acquisition (a power meter whose
    single read returns power1 + power2), don't give each meter its own read_fn —
    build them from a `Source` (see below) so the instrument is triggered once.
    """

    def __init__(self, name: str, read_fn=None, mode: str = "maximize",
                 target: Optional[float] = None, cost: float = 0.0,
                 group: Optional[str] = None, device: Optional[str] = None,
                 param: Optional[str] = None, source: Optional["Source"] = None,
                 key: Optional[str] = None, value_type: str = "scalar",
                 display_name: Optional[str] = None, unit: Optional[str] = None,
                 shape: Optional[tuple | list] = None, dtype: Optional[str] = None):
        self.name = name
        self._read_fn = read_fn
        self.mode = mode
        self.target = target
        self.cost = cost
        self.group = group
        self.device = device
        self.param = param
        self.source = source            # shared acquisition this channel comes from
        self.key = key or name          # this channel's key inside the source reading
        self.value_type = value_type
        self.display_name = display_name or name
        self.unit = unit
        self.shape = list(shape) if shape is not None else None
        self.dtype = dtype

    def get(self):
        if self.source is not None:
            value = self.source.read()[self.key]
            return float(value) if self.value_type == "scalar" else json_value(value)
        if self._read_fn is None:
            raise NotImplementedError(f"meter {self.name}: provide read_fn or override get()")
        value = self._read_fn()
        return float(value) if self.value_type == "scalar" else json_value(value)


class Source:
    """One instrument acquisition that yields SEVERAL channels at once.

    Some instruments hand you a whole reading per trigger — ``pm.read()`` returns
    ``{"power1": .., "power2": ..}``. Splitting that into independent meters would
    re-trigger the instrument once per channel (slow, and the channels would come
    from different acquisitions — inconsistent). A Source solves both: it reads
    once and caches, and every meter built from it serves from that cache.

    The evaluator invalidates the cache **before each averaging pass**, so the
    cache never spans two acquisitions — ``averages=8`` still costs 8 real reads
    and still averages 8 independent noise samples, it just doesn't multiply them
    by the number of channels you happen to want.

    Channels of one source default to the same timing ``group``, i.e. they are
    read concurrently and the acquisition is billed once (max, not sum). Selective
    reads still hold: a stage that needs none of a source's channels never
    triggers it at all.

        pm = Source("pm", lambda: dict(zip(("power1", "power2"), inst.read_both())),
                    cost=1.0, device="双通道光功率计")
        METERS = [pm.meter("y1", "power1"), pm.meter("y3", "power2")]
    """

    def __init__(self, name: str, read_fn, cost: float = 0.0,
                 group: Optional[str] = None, device: Optional[str] = None):
        self.name = name
        self._read_fn = read_fn
        self.cost = cost
        self.group = group or f"src:{name}"     # same acquisition ⇒ concurrent
        self.device = device
        self._cache: Optional[dict] = None

    def read(self) -> dict:
        """The current acquisition — triggers the instrument only on first use."""
        if self._cache is None:
            self._cache = dict(self._read_fn())
        return self._cache

    def invalidate(self) -> None:
        """Drop the cached acquisition; the next read() triggers the instrument."""
        self._cache = None

    def meter(self, name: str, key: Optional[str] = None, mode: str = "maximize",
              target: Optional[float] = None, cost: Optional[float] = None,
              group: Optional[str] = None, device: Optional[str] = None,
              param: Optional[str] = None, value_type: str = "scalar",
              display_name: Optional[str] = None, unit: Optional[str] = None,
              shape: Optional[tuple | list] = None, dtype: Optional[str] = None) -> Meter:
        """Expose one channel of this acquisition as an objective y."""
        return Meter(name, mode=mode, target=target,
                     cost=self.cost if cost is None else cost,
                     group=group or self.group,
                     device=device or self.device,
                     param=param or key or name,
                     source=self, key=key or name, value_type=value_type,
                     display_name=display_name, unit=unit, shape=shape, dtype=dtype)


# ---- evaluator backed by the user's axes + meters ----
class DeviceEvaluator:
    """Drives the user device: move axes, then read ONLY the requested meters.

    Same duck-typed API as Evaluator/HardwareEvaluator (evaluate(x, stage,
    channels) -> dict), so every algorithm / orchestrator / graph runs on it
    unchanged. Honours selective reads (a stage needing y1 alone never calls
    y2.get()), shared acquisitions (channels of one `Source` trigger the
    instrument once per pass), averaging, safety clamping, and the
    parallel/serial cost model.
    """

    def __init__(self, axes, meters, averages: int = 1,
                 safety: Optional[SafetyLimits] = None, settle_time: float = 0.0,
                 costs: Optional[dict] = None, groups: Optional[dict] = None):
        self._axes = {a.name: a for a in axes}
        self._meters = {m.name: m for m in meters}
        self._order = [m.name for m in meters]
        self.averages = max(1, averages)
        self.safety = safety
        self.settle_time = settle_time
        self.costs = costs or {}
        self.groups = groups or {}
        self.history: list[dict] = []
        self.reads: dict[str, int] = {}
        self.sim_seconds: float = 0.0

    def _sources(self, keys) -> list:
        """The distinct shared acquisitions backing `keys` (deduped by identity).

        Only the sources actually needed this step — a source whose channels are
        all unused is never invalidated and therefore never triggered."""
        out, seen = [], set()
        for k in keys:
            src = getattr(self._meters[k], "source", None)
            if src is not None and id(src) not in seen:
                seen.add(id(src))
                out.append(src)
        return out

    def evaluate(self, x: dict, stage: str = "", channels=None) -> dict:
        import time
        target = self.safety.enforce(x) if self.safety else x
        for name, val in target.items():
            if name in self._axes:
                self._axes[name].move(val)
        if self.settle_time:
            time.sleep(self.settle_time)
        keys = self._order if channels is None else [k for k in channels if k in self._meters]
        sources = self._sources(keys)
        samples = {k: [] for k in keys}
        for _ in range(self.averages):
            for src in sources:
                src.invalidate()          # each pass = a FRESH acquisition, so averaging
            for k in keys:                #   still samples independent noise
                samples[k].append(self._meters[k].get())       # only read what's needed
        import numpy as np
        y = {}
        for k, vals in samples.items():
            if getattr(self._meters[k], "value_type", "scalar") != "scalar":
                shapes = [np.asarray(v).shape for v in vals]
                if len(set(shapes)) != 1:
                    raise ValueError(f"array channel {k!r} changed shape while averaging: {shapes}")
                y[k] = np.mean(np.asarray(vals, dtype=float), axis=0).tolist()
            else:
                y[k] = sum(float(v) for v in vals) / len(vals)
        for k in y:
            self.reads[k] = self.reads.get(k, 0) + 1
        self.sim_seconds += self.averages * read_seconds(y.keys(), self.costs, self.groups)
        self.history.append({"stage": stage, **target, **y})
        return y


class DeviceSpec:
    """Parsed user device: builds the VOCS and an evaluator, reports current pose."""

    def __init__(self, axes, meters, source: str = ""):
        if not axes or not meters:
            raise ValueError("device must define at least one axis (x) and one meter (y)")
        self.axes = list(axes)
        self.meters = list(meters)
        self.source = source

    def vocs(self) -> VOCS:
        variables = {a.name: Variable(low=float(a.low), high=float(a.high),
                                      resolution=getattr(a, "resolution", None))
                     for a in self.axes}
        objectives = {}
        for m in self.meters:
            objectives[m.name] = Objective(
                mode=ObjectiveMode(getattr(m, "mode", "maximize") or "maximize"),
                target=getattr(m, "target", None),
                cost=float(getattr(m, "cost", 0.0) or 0.0),
                group=getattr(m, "group", None),
                device=getattr(m, "device", None),
                param=getattr(m, "param", None))
            objectives[m.name].value_type = ObjectiveValueType(
                getattr(m, "value_type", "scalar") or "scalar")
        return VOCS(variables=variables, objectives=objectives)

    def current_point(self) -> dict:
        """Where the axes are right now — the natural optimisation start point
        (real hardware has no absolute origin: you start from where you are)."""
        return {a.name: float(a.get()) for a in self.axes}

    def costs(self) -> dict:
        return {m.name: float(getattr(m, "cost", 0.0) or 0.0) for m in self.meters}

    def groups(self) -> dict:
        return {m.name: g for m in self.meters if (g := getattr(m, "group", None))}

    def evaluator(self, averages: int = 1, safety: Optional[SafetyLimits] = None,
                  settle_time: float = 0.0) -> DeviceEvaluator:
        return DeviceEvaluator(self.axes, self.meters, averages=averages, safety=safety,
                               settle_time=settle_time, costs=self.costs(), groups=self.groups())

    def info(self) -> dict:
        return {"source": self.source,
                "n_axes": len(self.axes), "n_meters": len(self.meters),
                "axes": [{"name": a.name, "low": float(a.low), "high": float(a.high),
                          "pos": float(a.get()), "resolution": getattr(a, "resolution", None),
                          "device": getattr(a, "device", None),
                          "param": getattr(a, "param", a.name)} for a in self.axes],
                "meters": [{"name": m.name, "mode": getattr(m, "mode", "maximize"),
                            "target": getattr(m, "target", None),
                            "cost": float(getattr(m, "cost", 0.0) or 0.0),
                            "group": getattr(m, "group", None),
                            "device": getattr(m, "device", None),
                            "param": getattr(m, "param", None),
                            "value_type": getattr(m, "value_type", "scalar"),
                            "display_name": getattr(m, "display_name", m.name),
                            "unit": getattr(m, "unit", None),
                            "shape": getattr(m, "shape", None),
                            "dtype": getattr(m, "dtype", None)}
                           for m in self.meters]}


# ---- loading ----
def _axes_meters(mod):
    if hasattr(mod, "build"):
        axes, meters = mod.build()
    else:
        axes = getattr(mod, "AXES", None)
        meters = getattr(mod, "METERS", None)
    if not axes or not meters:
        raise ValueError("device module must define AXES and METERS lists "
                         "(or a build() returning them)")
    return list(axes), list(meters)


def load_device(path: str) -> DeviceSpec:
    """Import a user device module from a file path and parse it into a DeviceSpec."""
    path = os.path.abspath(os.path.expanduser(path))
    spec = importlib.util.spec_from_file_location("optplat_userdevice", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load device module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                       # runs the user's file
    axes, meters = _axes_meters(mod)
    return DeviceSpec(axes, meters, source=path)
