"""Read a design out of OpticStudio so variables can be *picked*, not typed.

Defining a variable by hand means knowing the surface number, the PARM number
and the existing solves. Instead: open the file, read the Lens Data Editor, and
present a pick-list — "surface 3 (CoordinateBreak, 'align in') → Tilt About X" —
that turns straight into a `ZemaxKnob`.

    snap    = inspect_system(client)               # what's in the design
    choices = knob_choices(snap)                   # what could be a variable
    binding = build_binding(snap, [                # what the user picked
        {"name": "tilt_x", "surface": 3, "param": "tilt_x", "low": -1, "high": 1},
    ], merit=MeritSpec(total="merit"))

Followers ("伴随变量") are *discovered*, not declared: any surface whose PARM
carries a Pickup solve pointing at the picked cell is reported as a follower of
it, with the design's own scale/offset. A tilt-and-return pair authored in
OpticStudio therefore needs no extra configuration — selecting the master picks
up the return surface automatically.

Backed by zemax_get_system / zemax_get_surface_solves / zemax_set_surface_parameter
(read mode) — PARM values are not part of the get_surface payload, so they are
read per surface.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .zemax import (
    CB_PARAMS,
    Follower,
    MeritSpec,
    ZemaxBinding,
    ZemaxKnob,
    _check,
    _get,
    param_number,
)

COORDINATE_BREAK = "coordinatebreak"

# Display metadata for the Coordinate Break parameters, in PARM order.
CB_LABELS: dict[int, tuple[str, str]] = {
    1: ("Decenter X", "lens"),      # "lens" = whatever the design's length unit is
    2: ("Decenter Y", "lens"),
    3: ("Tilt About X", "deg"),
    4: ("Tilt About Y", "deg"),
    5: ("Tilt About Z", "deg"),
    6: ("Order", ""),               # discrete flag, not a continuous variable
}
CB_NAME_BY_NUMBER = {v: k for k, v in CB_PARAMS.items()}


class SolveInfo(BaseModel):
    solve_type: str = "Fixed"
    pickup_surface: Optional[int] = None
    pickup_column: Optional[int] = None
    scale_factor: Optional[float] = None
    offset: Optional[float] = None

    @property
    def is_pickup(self) -> bool:
        return "pickup" in self.solve_type.lower() and self.pickup_surface is not None


class SurfaceInfo(BaseModel):
    number: int
    surface_type: str = "Standard"
    comment: str = ""
    radius: float = 0.0
    thickness: float = 0.0
    material: Optional[str] = None
    semi_diameter: float = 0.0
    conic: float = 0.0
    is_stop: bool = False
    params: dict[int, float] = Field(default_factory=dict)          # PARM -> value
    param_solves: dict[int, SolveInfo] = Field(default_factory=dict)
    thickness_solve: Optional[SolveInfo] = None

    @property
    def is_coordinate_break(self) -> bool:
        return self.surface_type.lower().replace(" ", "") == COORDINATE_BREAK


class SystemSnapshot(BaseModel):
    """What the platform knows about the currently open design."""
    file: Optional[str] = None
    title: str = ""
    units: str = "mm"
    n_surfaces: int = 0
    aperture: dict[str, Any] = Field(default_factory=dict)
    surfaces: list[SurfaceInfo] = Field(default_factory=list)
    operands: list[dict] = Field(default_factory=list)              # MFE rows
    n_fields: int = 0
    n_wavelengths: int = 0

    def surface(self, number: int) -> SurfaceInfo:
        for s in self.surfaces:
            if s.number == number:
                return s
        raise KeyError(f"surface {number} is not in this design")


class KnobChoice(BaseModel):
    """One pickable variable, ready to be turned into a ZemaxKnob."""
    surface: int
    kind: str = "param"
    param: Optional[str] = None            # canonical name, e.g. "tilt_x"
    param_number: Optional[int] = None
    label: str = ""                        # "S3 · align in · Tilt About X"
    unit: str = ""
    current: float = 0.0
    discrete: bool = False                 # e.g. Coordinate Break Order
    followers: list[Follower] = Field(default_factory=list)   # discovered pickups

    def id(self) -> str:
        return f"s{self.surface}.{self.param or self.kind}"

    def to_knob(self, low: float, high: float, scale: float = 1.0,
                offset: float = 0.0, followers: Optional[list[Follower]] = None) -> ZemaxKnob:
        return ZemaxKnob(
            surface=self.surface, kind=self.kind, param=self.param,
            low=low, high=high, scale=scale, offset=offset,
            followers=list(self.followers if followers is None else followers),
        )


# ---- reading the design -----------------------------------------------------
def inspect_system(client: Any,
                   surfaces: Optional[list[int]] = None,
                   include_params: bool = True,
                   include_solves: bool = True,
                   include_merit: bool = True) -> SystemSnapshot:
    """Snapshot the open design (optionally limited to `surfaces`)."""
    raw = client.call_tool("zemax_get_system", {"includeSurfaces": True,
                                                "includeFields": True,
                                                "includeWavelengths": True})
    _check(raw, "get system")

    rows = _get(raw, "surfaces", []) or []
    infos: list[SurfaceInfo] = []
    for row in rows:
        number = int(_get(row, "number", 0))
        if surfaces is not None and number not in surfaces:
            continue
        info = SurfaceInfo(
            number=number,
            surface_type=str(_get(row, "surfaceType", "Standard") or "Standard"),
            comment=str(_get(row, "comment", "") or ""),
            radius=float(_get(row, "radius", 0.0) or 0.0),
            thickness=float(_get(row, "thickness", 0.0) or 0.0),
            material=_get(row, "material"),
            semi_diameter=float(_get(row, "semiDiameter", 0.0) or 0.0),
            conic=float(_get(row, "conic", 0.0) or 0.0),
            is_stop=bool(_get(row, "isStop", False)),
        )
        if include_params and info.is_coordinate_break:
            info.params = read_params(client, number)
        if include_solves:
            info.param_solves, info.thickness_solve = read_solves(client, number)
        infos.append(info)

    snap = SystemSnapshot(
        file=_get(raw, "filePath"),
        title=str(_get(raw, "title", "") or ""),
        units=str(_get(raw, "units", "mm") or "mm"),
        n_surfaces=int(_get(raw, "numberOfSurfaces", len(rows)) or len(rows)),
        aperture=_get(raw, "aperture", {}) or {},
        surfaces=infos,
        n_fields=len(_get(raw, "fields", []) or []),
        n_wavelengths=len(_get(raw, "wavelengths", []) or []),
    )
    if include_merit:
        mf = client.call_tool("zemax_get_merit_function", {"includeValues": True})
        _check(mf, "get merit function")
        snap.operands = list(_get(mf, "operands", []) or [])
    return snap


def read_params(client: Any, surface: int) -> dict[int, float]:
    """Current PARM values of a surface (read mode of zemax_set_surface_parameter)."""
    res = client.call_tool("zemax_set_surface_parameter", {"surfaceNumber": surface})
    _check(res, f"read surface {surface} parameters")
    out: dict[int, float] = {}
    for entry in _get(res, "parameters", []) or []:
        out[int(_get(entry, "number", 0))] = float(_get(entry, "value", 0.0) or 0.0)
    return out


def read_solves(client: Any, surface: int) -> tuple[dict[int, SolveInfo], Optional[SolveInfo]]:
    """Solves of a surface — this is where authored pickups are found."""
    res = client.call_tool("zemax_get_surface_solves", {"surfaceNumber": surface})
    _check(res, f"read surface {surface} solves")
    params: dict[int, SolveInfo] = {}
    for key, data in (_get(res, "parameters", {}) or {}).items():
        if isinstance(data, dict):
            params[int(key)] = _solve(data)
    thickness = _get(res, "thickness")
    return params, _solve(thickness) if isinstance(thickness, dict) else None


def _solve(data: dict) -> SolveInfo:
    return SolveInfo(
        solve_type=str(_get(data, "solveType", "Fixed") or "Fixed"),
        pickup_surface=_opt_int(_get(data, "pickupSurface")),
        pickup_column=_opt_int(_get(data, "pickupColumn")),
        scale_factor=_opt_float(_get(data, "scaleFactor")),
        offset=_opt_float(_get(data, "offset")),
    )


# ---- turning the design into pickable variables -----------------------------
def knob_choices(snap: SystemSnapshot,
                 include_thickness: bool = True,
                 include_order: bool = False) -> list[KnobChoice]:
    """Every cell in the design that could reasonably become a variable."""
    length_unit = snap.units or "mm"
    choices: list[KnobChoice] = []
    for surf in snap.surfaces:
        tag = f"S{surf.number}" + (f" · {surf.comment}" if surf.comment else "")
        if surf.is_coordinate_break:
            for number, (label, unit) in CB_LABELS.items():
                if number == 6 and not include_order:
                    continue
                name = CB_NAME_BY_NUMBER[number]
                choices.append(KnobChoice(
                    surface=surf.number, kind="param", param=name, param_number=number,
                    label=f"{tag} · {label}",
                    unit=length_unit if unit == "lens" else unit,
                    current=surf.params.get(number, 0.0),
                    discrete=(number == 6),
                    followers=detect_followers(snap, surf.number, number),
                ))
        if include_thickness and surf.number != snap.n_surfaces - 1:
            choices.append(KnobChoice(
                surface=surf.number, kind="thickness", param=None,
                label=f"{tag} · Thickness", unit=length_unit,
                current=surf.thickness,
                followers=detect_followers(snap, surf.number, None),
            ))
    return choices


def detect_followers(snap: SystemSnapshot, surface: int,
                     param: Optional[int]) -> list[Follower]:
    """Surfaces whose cells pick up from (surface, param) — the 伴随变量.

    A tilt/decenter-and-return pair authored in OpticStudio shows up here on its
    own. `mode="pickup"` is reported because the design already maintains the
    relation; the platform then only has to verify it refreshed (see
    `RefreshSpec.followers`).
    """
    def follower(other: SurfaceInfo, solve: SolveInfo,
                 number: Optional[int]) -> Follower:
        return Follower(
            surface=other.number,
            kind="param" if number is not None else "thickness",
            param=CB_NAME_BY_NUMBER.get(number, number) if number is not None else None,
            scale=solve.scale_factor if solve.scale_factor is not None else 1.0,
            offset=solve.offset or 0.0,
            mode="pickup",
            pickup_column=solve.pickup_column,
        )

    if param is None:                       # thickness picked up by another surface
        return [follower(o, o.thickness_solve, None) for o in snap.surfaces
                if o.number != surface and o.thickness_solve is not None
                and o.thickness_solve.is_pickup
                and o.thickness_solve.pickup_surface == surface]

    # `pickup_column` addresses the source cell, but its numbering differs between
    # OpticStudio builds (bare PARM number vs. an absolute column enum). Trust an
    # exact match first; if nothing matches, fall back to "same PARM index", which
    # is what any constant-offset numbering preserves. Never both — a loose match
    # would invent followers that do not exist.
    exact: list[Follower] = []
    same_index: list[Follower] = []
    for other in snap.surfaces:
        if other.number == surface:
            continue
        for number, solve in other.param_solves.items():
            if not solve.is_pickup or solve.pickup_surface != surface:
                continue
            if solve.pickup_column == param:
                exact.append(follower(other, solve, number))
            elif number == param:
                same_index.append(follower(other, solve, number))
    return exact or same_index


def build_binding(snap: SystemSnapshot,
                  selections: list[dict],
                  merit: Optional[MeritSpec] = None) -> ZemaxBinding:
    """Assemble a ZemaxBinding from what the user picked.

    Each selection: {"name", "surface", "param" | "kind", "low", "high",
                     "scale"?, "offset"?, "followers"?}
    `followers` defaults to whatever the design already declares (auto-detected);
    pass an explicit list to override, or [] to drive the master alone.
    """
    by_id = {c.id(): c for c in knob_choices(snap, include_order=True)}
    knobs: dict[str, ZemaxKnob] = {}
    for sel in selections:
        name = sel["name"]
        surface = int(sel["surface"])
        kind = sel.get("kind", "param" if sel.get("param") is not None else "thickness")
        param = sel.get("param")
        key = f"s{surface}.{param if kind == 'param' else kind}"
        choice = by_id.get(key)
        if choice is None:                       # not offered as a choice: still valid
            choice = KnobChoice(surface=surface, kind=kind,
                                param=str(param) if param is not None else None,
                                param_number=param_number(param) if kind == "param" else None,
                                followers=detect_followers(
                                    snap, surface,
                                    param_number(param) if kind == "param" else None))
        followers = sel.get("followers")
        knobs[name] = choice.to_knob(
            low=float(sel["low"]), high=float(sel["high"]),
            scale=float(sel.get("scale", 1.0)), offset=float(sel.get("offset", 0.0)),
            followers=None if followers is None else [Follower(**f) for f in followers],
        )
    return ZemaxBinding(knobs=knobs, merit=merit or MeritSpec())


def operand_choices(snap: SystemSnapshot) -> list[dict]:
    """MFE rows, as a pick-list for objectives."""
    out = []
    for i, op in enumerate(snap.operands, start=1):
        row = int(_get(op, "row", i))
        op_type = str(_get(op, "type", "") or "")
        out.append({
            "row": row, "type": op_type,
            "label": f"行{row} · {op_type}",
            "value": float(_get(op, "value", 0.0) or 0.0),
            "target": float(_get(op, "target", 0.0) or 0.0),
            "weight": float(_get(op, "weight", 0.0) or 0.0),
        })
    return out


def main() -> None:
    """CLI: print what the open design offers as variables.

        python -m optplat.zemax_inspect "C:/…/OpticStudioMCPServer.exe" "C:/lens.zmx"
        python -m optplat.zemax_inspect            # offline, against the fake server
    """
    import sys

    from .mcp_client import McpStdioClient

    args = sys.argv[1:]
    command = [args[0]] if args else [sys.executable, "-m", "optplat.zemax_sim", "--pickups"]
    with McpStdioClient(command, timeout=120) as client:
        client.call_tool("zemax_connect", {"mode": "extension"})
        if len(args) > 1:
            client.call_tool("zemax_open_file", {"filePath": args[1]})
        snap = inspect_system(client)
        print(f"{snap.title or snap.file or '(untitled)'} · {snap.n_surfaces} 面 · "
              f"{snap.units} · {snap.n_fields} 视场 · {snap.n_wavelengths} 波长")
        print("\n可选自变量：")
        for c in knob_choices(snap):
            follow = ("  跟随→ " + ", ".join(f"面{f.surface}(×{f.scale:g})" for f in c.followers)
                      if c.followers else "")
            print(f"  {c.id():22} {c.label:36} 当前={c.current:<10.4g} [{c.unit or '-'}]{follow}")
        print("\n可选目标（Merit Function Editor）：")
        for op in operand_choices(snap):
            print(f"  {op['label']:16} value={op['value']:<12.6g} "
                  f"target={op['target']:g} weight={op['weight']:g}")


def _opt_int(v) -> Optional[int]:
    return None if v is None else int(v)


def _opt_float(v) -> Optional[float]:
    return None if v is None else float(v)


if __name__ == "__main__":
    main()
