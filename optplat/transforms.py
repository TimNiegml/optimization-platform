"""Deterministic measurement transforms used by graph data_transform nodes."""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from .evaluator import json_value


SCALAR_OPERATIONS = {"mean", "max", "min", "std", "element"}
BINARY_OPERATIONS = {"add", "subtract", "multiply", "divide"}


def _select(value, selector: dict | None):
    arr = np.asarray(value, dtype=float)
    selector = selector or {}
    if "indices" in selector:
        arr = arr[tuple(int(i) for i in selector["indices"])]
    elif any(k in selector for k in ("start", "stop", "step")):
        arr = arr[slice(selector.get("start"), selector.get("stop"), selector.get("step"))]
    return arr


def transform_value(value, operation: str, params: dict | None = None):
    """Apply one JSON-safe numeric transform to a scalar/vector/matrix value."""
    params = params or {}
    arr = np.asarray(value, dtype=float)
    if not np.all(np.isfinite(arr)):
        raise ValueError("data transform input contains NaN or infinity")
    if operation == "identity":
        result = arr
    elif operation == "transpose":
        result = arr.T
    elif operation == "flatten":
        result = arr.reshape(-1)
    elif operation == "row_mean":
        if arr.ndim != 2:
            raise ValueError("row_mean requires a 2-D input")
        result = arr.mean(axis=1)
    elif operation == "column_mean":
        if arr.ndim != 2:
            raise ValueError("column_mean requires a 2-D input")
        result = arr.mean(axis=0)
    elif operation in SCALAR_OPERATIONS:
        if operation == "element":
            indices = params.get("indices")
            if indices is None:
                indices = [params.get("index", 0)] if arr.ndim == 1 else [params.get("row", 0), params.get("column", 0)]
            indices = tuple(int(i) for i in indices)
            if len(indices) != arr.ndim:
                raise ValueError(f"element requires {arr.ndim} indices, got {len(indices)}")
            try:
                result = arr[indices]
            except IndexError as exc:
                raise ValueError(f"element index {indices} is outside input shape {arr.shape}") from exc
        else:
            result = getattr(arr, operation)()
    elif operation == "normalize_minmax":
        spread = arr.max() - arr.min()
        result = np.zeros_like(arr) if spread == 0 else (arr - arr.min()) / spread
    elif operation in {"select_row", "select_column"}:
        if arr.ndim != 2:
            raise ValueError(f"{operation} requires a 2-D input")
        index = int(params.get("index", 0))
        result = arr[index] if operation == "select_row" else arr[:, index]
    elif operation == "roi":
        if arr.ndim != 2:
            raise ValueError("roi requires a 2-D input")
        r0, r1 = int(params.get("row_start", 0)), int(params.get("row_end", arr.shape[0]))
        c0, c1 = int(params.get("column_start", 0)), int(params.get("column_end", arr.shape[1]))
        result = arr[r0:r1, c0:c1]
        if result.size == 0:
            raise ValueError("roi produced an empty matrix")
    else:
        raise ValueError(f"unsupported data transform operation: {operation!r}")
    return json_value(result)


def transform_inputs(values: list, operation: str, selectors: list | None = None,
                     params: dict | None = None):
    """Apply a unary transform or a shape-safe element-wise binary transform."""
    selectors = selectors or [{} for _ in values]
    selected = [_select(v, selectors[i] if i < len(selectors) else {})
                for i, v in enumerate(values)]
    if operation not in BINARY_OPERATIONS:
        return transform_value(selected[0], operation, params)
    if len(selected) != 2:
        raise ValueError(f"{operation} requires exactly two inputs")
    if selected[0].shape != selected[1].shape:
        raise ValueError(f"{operation} input shapes do not match: "
                         f"{selected[0].shape} vs {selected[1].shape}")
    fn = {"add": np.add, "subtract": np.subtract,
          "multiply": np.multiply, "divide": np.divide}[operation]
    with np.errstate(divide="raise", invalid="raise"):
        try:
            result = fn(selected[0], selected[1])
        except FloatingPointError as exc:
            raise ValueError(f"{operation} produced an invalid numeric result") from exc
    return json_value(result)


def _inputs(spec: dict) -> list[dict]:
    return list(spec.get("inputs") or [{"channel": spec.get("input"),
                                        "selector": spec.get("selector", {})}])


class TransformEvaluator:
    """Evaluator decorator that exposes transformed channels on demand."""

    def __init__(self, evaluator, transforms: Iterable[dict], known_channels: Iterable[str]):
        self.evaluator = evaluator
        self.transforms: dict[str, dict] = {}
        known = set(known_channels)
        for spec in transforms:
            output, inputs = spec.get("output"), _inputs(spec)
            if not output or not inputs or any(not i.get("channel") for i in inputs):
                raise ValueError("data_transform requires non-empty input(s) and output channels")
            if output in known:
                raise ValueError(f"data_transform output already exists: {output!r}")
            unknown = [i["channel"] for i in inputs if i["channel"] not in known]
            if unknown:
                raise ValueError(f"data_transform {output!r} references unknown input: {unknown[0]!r}")
            self.transforms[output] = dict(spec)
            known.add(output)
        self.known = known
        self.latest_values: dict = {}

    def _physical(self, name: str) -> set[str]:
        if name not in self.transforms:
            return {name}
        return set().union(*(self._physical(i["channel"])
                             for i in _inputs(self.transforms[name])))

    def _calculate(self, name: str, values: dict):
        if name in values:
            return values[name]
        spec = self.transforms[name]
        inputs = _inputs(spec)
        for item in inputs:
            if item["channel"] in self.transforms:
                self._calculate(item["channel"], values)
        values[name] = transform_inputs(
            [values[i["channel"]] for i in inputs], spec.get("operation", "identity"),
            [i.get("selector", {}) for i in inputs], spec.get("params"))
        return values[name]

    def enrich_available(self, values: dict) -> dict:
        for name, spec in self.transforms.items():
            inputs = _inputs(spec)
            if all(i["channel"] in values for i in inputs):
                values[name] = transform_inputs(
                    [values[i["channel"]] for i in inputs], spec.get("operation", "identity"),
                    [i.get("selector", {}) for i in inputs], spec.get("params"))
        return values

    def evaluate(self, x: dict[str, float], stage: str = "", channels=None) -> dict:
        requested = set(self.known if channels is None else channels)
        physical = set().union(*(self._physical(k) for k in requested))
        values = self.evaluator.evaluate(x, stage=stage, channels=physical)
        for name in requested:
            if name in self.transforms:
                self._calculate(name, values)
        self.latest_values = dict(values)
        result = {k: values[k] for k in requested if k in values}
        if self.evaluator.history:
            self.evaluator.history[-1].update(result)
        return result

    @property
    def history(self): return self.evaluator.history

    @property
    def reads(self): return self.evaluator.reads

    @property
    def sim_seconds(self): return self.evaluator.sim_seconds
