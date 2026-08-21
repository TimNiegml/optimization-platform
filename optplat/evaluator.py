"""Evaluator = the接入 layer.

Wraps whatever actually produces measurements: a Python function today, a
hardware adapter (motor stage + power meter) tomorrow. The contract is a plain
dict in -> dict out, so the generator/orchestrator never know or care whether
`f` is a simulation or a real device.

An optional `store` (SQLiteStore) durably archives every evaluation for
resume / rollback.
"""
from __future__ import annotations

import ast
import math
from typing import Callable, Iterable, Optional


_DERIVED_FUNCS = {"abs": abs, "min": min, "max": max}
_DERIVED_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b, ast.Mod: lambda a, b: a % b,
}
_DERIVED_UNARY = {ast.UAdd: lambda a: a, ast.USub: lambda a: -a}


def json_value(value):
    """Normalize numpy/array-like measurement values for traces and REST JSON."""
    if hasattr(value, "to_numpy"):  # pandas DataFrame/Series, without a pandas dependency
        value = value.to_numpy()
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, list):
        return [json_value(v) for v in value]
    if hasattr(value, "item"):
        value = value.item()
    return value


def _expression_names(expression: str) -> set[str]:
    """Return channel names referenced by a derived-output expression."""
    tree = ast.parse(expression, mode="eval")
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
            and n.id not in _DERIVED_FUNCS}


def _eval_expression(expression: str, values: dict[str, float]) -> float:
    """Evaluate the small arithmetic DSL used for derived outputs.

    Deliberately does not use Python ``eval``. Only numbers, channel names,
    arithmetic, and ``abs/min/max`` are accepted.
    """
    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id in values:
            return float(values[node.id])
        if isinstance(node, ast.BinOp) and type(node.op) in _DERIVED_BINOPS:
            return _DERIVED_BINOPS[type(node.op)](visit(node.left), visit(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _DERIVED_UNARY:
            return _DERIVED_UNARY[type(node.op)](visit(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _DERIVED_FUNCS and not node.keywords):
            return _DERIVED_FUNCS[node.func.id](*(visit(a) for a in node.args))
        raise ValueError(f"unsupported derived-output expression element: {ast.dump(node)}")

    result = float(visit(ast.parse(expression, mode="eval")))
    if not math.isfinite(result):
        raise ValueError(f"derived-output expression produced a non-finite value: {expression}")
    return result


class DerivedEvaluator:
    """Decorator that turns measured channels into calculated objectives.

    It expands a request for ``z1`` into the physical channels referenced by
    its expression, asks the wrapped evaluator to read only those channels,
    and then calculates ``z1``. Expressions may reference other derived
    outputs; unknown names and dependency cycles fail before hardware moves.
    """

    def __init__(self, evaluator, expressions: dict[str, str], known_channels: Iterable[str]):
        self.evaluator = evaluator
        self.expressions = {k: v.strip() for k, v in expressions.items() if v and v.strip()}
        self.known = set(known_channels)
        self.dependencies = {k: _expression_names(v) for k, v in self.expressions.items()}
        self.latest_values: dict = {}
        for name, deps in self.dependencies.items():
            unknown = deps - self.known
            if unknown:
                raise ValueError(f"derived output {name!r} references unknown channels: {sorted(unknown)}")
        # Resolve every derived channel now, so cycles are reported before a run.
        for name in self.expressions:
            self._physical_dependencies(name, set())

    def _physical_dependencies(self, name: str, visiting: set[str]) -> set[str]:
        if name not in self.expressions:
            return {name}
        if name in visiting:
            raise ValueError(f"cyclic derived-output dependency involving {name!r}")
        return set().union(*(self._physical_dependencies(dep, visiting | {name})
                             for dep in self.dependencies[name])) if self.dependencies[name] else set()

    def _calculate(self, name: str, values: dict[str, float], visiting: set[str]) -> float:
        if name in values:
            return values[name]
        if name in visiting:
            raise ValueError(f"cyclic derived-output dependency involving {name!r}")
        for dep in self.dependencies[name]:
            if dep in self.expressions:
                values[dep] = self._calculate(dep, values, visiting | {name})
        values[name] = _eval_expression(self.expressions[name], values)
        return values[name]

    def enrich_available(self, values: dict) -> dict:
        """Refresh every derived output whose dependencies are available.

        ``StageEngine.last_y`` is the live latest-value cache.  A selective
        acquisition may read only y1, but z1=y1+y2 must still be refreshed from
        the new y1 and the latest y2 rather than leaving yesterday's z1 on the
        live panel.  Outputs with a dependency never measured yet are skipped.
        """
        for name in self.expressions:
            physical = self._physical_dependencies(name, set())
            if physical <= set(values):
                # Remove stale intermediate/result values so the whole derived
                # dependency chain is recalculated from the latest physical y.
                refreshed = dict(values)
                for derived in self.expressions:
                    refreshed.pop(derived, None)
                try:
                    self._calculate(name, refreshed, set())
                except KeyError:
                    continue
                values[name] = refreshed[name]
        return values

    def evaluate(self, x: dict[str, float], stage: str = "", channels=None) -> dict[str, float]:
        requested = set(self.known if channels is None else channels)
        physical = set().union(*(self._physical_dependencies(k, set()) for k in requested))
        values = self.evaluator.evaluate(x, stage=stage, channels=physical)
        for name in requested:
            if name in self.expressions:
                self._calculate(name, values, set())
        self.latest_values = dict(values)
        result = {k: values[k] for k in requested if k in values}
        # Keep calculated values in the shared trace/CSV without counting them
        # as instrument reads or charging an extra measurement cost.
        if self.evaluator.history:
            self.evaluator.history[-1].update(result)
        return result

    @property
    def history(self):
        return self.evaluator.history

    @property
    def reads(self):
        return self.evaluator.reads

    @property
    def sim_seconds(self):
        return self.evaluator.sim_seconds


def with_derived_outputs(evaluator, vocs):
    expressions = {name: obj.expression for name, obj in vocs.objectives.items()
                   if obj.expression}
    if not expressions or isinstance(evaluator, DerivedEvaluator):
        return evaluator
    return DerivedEvaluator(evaluator, expressions, vocs.objectives)


def read_seconds(keys: Iterable[str], costs: dict[str, float],
                 groups: Optional[dict[str, str]] = None) -> float:
    """Modelled time to measure `keys` in ONE acquisition, honouring which
    instruments run concurrently.

    `groups` maps a channel → a group id. Channels in the SAME group are read
    **in parallel** (光功率 + PDL 同表同测), so the group costs `max` of its
    members; different groups are read **serially** (光功率 vs 中心波长 换设备),
    so groups sum. A channel with no group entry is its own serial group.
    `groups` empty/None ⇒ everything serial (plain sum, back-compatible).
    """
    if not groups:
        return sum(costs.get(k, 0.0) for k in keys)
    by_group: dict[str, float] = {}
    for k in keys:
        g = groups.get(k, f"__{k}")            # ungrouped channel = its own group
        by_group[g] = max(by_group.get(g, 0.0), costs.get(k, 0.0))
    return sum(by_group.values())


class Evaluator:
    """Function-backed evaluator with per-channel (selective) reads + cost.

    `evaluate(x, stage, channels)` measures ONLY the requested objective
    channels — a stage that needs y1 alone never reads (or pays for) y2. Each
    channel carries a `cost` (seconds); the evaluator accumulates per-channel
    read counts and total simulated seconds so a workflow's measurement budget
    is visible. `channels=None` reads everything (back-compatible).

    `groups` models instrument concurrency: same-group channels measure in
    parallel (time = max), different groups serially (time sums) — see
    `read_seconds`.
    """

    def __init__(self, func: Callable[[dict[str, float]], dict[str, float]],
                 store: Optional[object] = None,
                 costs: Optional[dict[str, float]] = None,
                 groups: Optional[dict[str, str]] = None):
        self.func = func
        self.store = store
        self.costs = costs or {}
        self.groups = groups or {}
        self.history: list[dict] = []       # every evaluation, for archiving
        self.reads: dict[str, int] = {}     # per-channel read counter
        self.sim_seconds: float = 0.0       # total modelled measurement time

    def evaluate(self, x: dict[str, float], stage: str = "",
                 channels: Optional[Iterable[str]] = None) -> dict[str, float]:
        y_all = {k: json_value(v) for k, v in self.func(x).items()}
        y = y_all if channels is None else {k: y_all[k] for k in channels if k in y_all}
        for k in y:
            self.reads[k] = self.reads.get(k, 0) + 1
        self.sim_seconds += read_seconds(y.keys(), self.costs, self.groups)
        self.history.append({"stage": stage, **x, **y})
        if self.store is not None:
            self.store.append(stage, x, y)
        return y
