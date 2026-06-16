"""S-expression / JSON parsers for the data kicad-cli does not hand us directly.

Covers three sources:
  * ``.kicad_pcb``  -> tracks, vias, footprints, nets, copper stackup (sexpdata)
  * ``.kicad_pro``  -> net classes, design rules, ERC severities (plain JSON)
  * ``.net`` netlist -> components, pins with types, nets (sexpdata)

Read-only. We never write these files in v0.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import json
import math
from pathlib import Path

import sexpdata

OZ_TO_MM = 0.035  # 1 oz/ft^2 finished copper = 35 µm (KiCad's convention)


def _sym(x):
    return x.value() if isinstance(x, sexpdata.Symbol) else x


def _head(node) -> str | None:
    if isinstance(node, list) and node:
        return _sym(node[0])
    return None


def _get(node, key):
    """First direct child list whose head symbol == key."""
    if isinstance(node, list):
        for child in node[1:]:
            if isinstance(child, list) and _head(child) == key:
                return child
    return None


def _getval(node, key, default=None):
    child = _get(node, key)
    if child and len(child) > 1:
        return _sym(child[1])
    return default


def _getall(node, key):
    return [c for c in (node[1:] if isinstance(node, list) else []) if _head(c) == key]


# --------------------------------------------------------------------------- #
# .kicad_pcb
# --------------------------------------------------------------------------- #
@dataclass
class Track:
    width: float
    layer: str
    net: int
    length: float = 0.0  # routed length (mm); used to ignore short pad-fanout stubs


@dataclass
class Via:
    size: float
    drill: float
    net: int


@dataclass
class Footprint:
    ref: str
    value: str
    layer: str
    x: float
    y: float
    nets: set = field(default_factory=set)  # net ids touched by its pads


@dataclass
class Board:
    nets: dict  # id -> name
    tracks: list  # Track
    vias: list  # Via
    footprints: list  # Footprint
    copper_layers: int
    copper_oz: float  # outer copper weight estimate
    poured_nets: set = field(default_factory=set)  # net NAMES carried by a copper zone
    raw_setup: dict = field(default_factory=dict)

    def net_name(self, nid: int) -> str:
        return self.nets.get(nid, f"<net{nid}>")


def parse_board(pcb_path: str | Path) -> Board:
    data = sexpdata.loads(Path(pcb_path).read_text(encoding="utf-8"))

    nets: dict[int, str] = {}
    for n in _getall(data, "net"):
        if len(n) >= 3:
            nets[int(_sym(n[1]))] = _sym(n[2])

    tracks: list[Track] = []
    vias: list[Via] = []
    footprints: list[Footprint] = []
    poured_nets: set[str] = set()

    def _pt(parent, key):
        c = _get(parent, key)
        if c and len(c) >= 3:
            return (float(_sym(c[1])), float(_sym(c[2])))
        return None

    for node in data[1:]:
        h = _head(node)
        if h == "zone":
            # a copper pour carries a net's bulk current; record its net name so the
            # trace-width check does not flag a poured net as "undersized" off its
            # thin track stubs alone.
            nn = _getval(node, "net_name")
            if nn:
                poured_nets.add(str(nn))
            continue
        if h in ("segment", "arc"):
            # both straight segments and curved (arc) tracks are conductors
            a, b = _pt(node, "start"), _pt(node, "end")
            length = math.dist(a, b) if a and b else 0.0
            tracks.append(
                Track(
                    width=float(_getval(node, "width", 0.0)),
                    layer=str(_getval(node, "layer", "")),
                    net=int(_getval(node, "net", -1)),
                    length=length,
                )
            )
        elif h == "via":
            size = _getval(node, "size", 0.0)
            drill = _getval(node, "drill", 0.0)
            vias.append(
                Via(size=float(size), drill=float(drill), net=int(_getval(node, "net", -1)))
            )
        elif h == "footprint":
            ref = value = ""
            for prop in _getall(node, "property"):
                if len(prop) >= 3 and _sym(prop[1]) == "Reference":
                    ref = _sym(prop[2])
                elif len(prop) >= 3 and _sym(prop[1]) == "Value":
                    value = _sym(prop[2])
            at = _get(node, "at")
            x = float(_sym(at[1])) if at and len(at) > 1 else 0.0
            y = float(_sym(at[2])) if at and len(at) > 2 else 0.0
            fp_nets = set()
            for pad in _getall(node, "pad"):
                pn = _get(pad, "net")
                if pn and len(pn) > 1:
                    fp_nets.add(int(_sym(pn[1])))
            footprints.append(
                Footprint(
                    ref=ref,
                    value=value,
                    layer=str(_getval(node, "layer", "")),
                    x=x,
                    y=y,
                    nets=fp_nets,
                )
            )

    copper_layers, copper_oz = _parse_stackup(data)
    return Board(
        nets=nets,
        tracks=tracks,
        vias=vias,
        footprints=footprints,
        copper_layers=copper_layers,
        copper_oz=copper_oz,
        poured_nets=poured_nets,
    )


def _parse_stackup(data) -> tuple[int, float]:
    """Return (copper layer count, outer copper weight in oz). Defaults 2 / 1oz."""
    setup = _get(data, "setup")
    copper_layers = 2
    copper_oz = 1.0
    layers = _get(data, "layers")
    if layers:
        # count layer entries whose name ends in ".Cu"
        names = [str(_sym(le[1])) for le in layers[1:] if isinstance(le, list) and len(le) > 1]
        cu = [n for n in names if n.endswith(".Cu")]
        if cu:
            copper_layers = len(cu)
    if setup:
        stack = _get(setup, "stackup")
        if stack:
            for lyr in _getall(stack, "layer"):
                name = _sym(lyr[1]) if len(lyr) > 1 else ""
                if isinstance(name, str) and name.endswith(".Cu"):
                    th = _getval(lyr, "thickness")
                    if th:
                        with contextlib.suppress(TypeError, ValueError):
                            copper_oz = round(float(th) / OZ_TO_MM, 2)
                        break
    return copper_layers, copper_oz


# --------------------------------------------------------------------------- #
# .kicad_pro
# --------------------------------------------------------------------------- #
@dataclass
class ProjectSettings:
    net_classes: list  # list of dicts (name, track_width, ...)
    net_class_assignments: dict  # pattern/net -> class name
    design_rules: dict
    erc_severities: dict  # rule -> severity (only overridden ones)


def parse_pro(pro_path: str | Path) -> ProjectSettings:
    d = json.loads(Path(pro_path).read_text(encoding="utf-8"))
    ns = d.get("net_settings", {})
    classes = ns.get("classes", [])
    assignments = {}
    for a in ns.get("netclass_assignments", []) or []:
        if isinstance(a, dict):
            assignments[a.get("pattern", a.get("net", ""))] = a.get("netclass", "")
    rules = d.get("board", {}).get("design_settings", {}).get("rules", {})
    erc = d.get("erc", {}).get("rule_severities", {})
    overridden = {k: v for k, v in erc.items() if v in ("ignore", "warning")}
    return ProjectSettings(
        net_classes=classes,
        net_class_assignments=assignments,
        design_rules=rules,
        erc_severities=overridden,
    )


# --------------------------------------------------------------------------- #
# .net netlist (KiCad S-expr)
# --------------------------------------------------------------------------- #
@dataclass
class Netlist:
    components: list  # dicts: ref, value, footprint
    nets: list  # dicts: name, code, nodes=[{ref, pin, type}]
    pin_types: dict  # (libpart) -> {pin_num: type}; keyed by 'lib:part'


def parse_netlist(net_path: str | Path) -> Netlist:
    data = sexpdata.loads(Path(net_path).read_text(encoding="utf-8"))

    components = []
    comps = _get(data, "components")
    if comps:
        for comp in _getall(comps, "comp"):
            ref = _getval(comp, "ref", "")
            components.append(
                {
                    "ref": ref,
                    "value": _getval(comp, "value", ""),
                    "footprint": _getval(comp, "footprint", ""),
                }
            )

    # libpart pin types: map "lib:part" -> {pin: type}
    pin_types: dict[str, dict] = {}
    libparts = _get(data, "libparts")
    if libparts:
        for lp in _getall(libparts, "libpart"):
            lib = _getval(lp, "lib", "")
            part = _getval(lp, "part", "")
            key = f"{lib}:{part}"
            pins = _get(lp, "pins")
            d = {}
            if pins:
                for pin in _getall(pins, "pin"):
                    d[str(_getval(pin, "num", ""))] = str(_getval(pin, "type", ""))
            pin_types[key] = d

    nets = []
    netsnode = _get(data, "nets")
    if netsnode:
        for net in _getall(netsnode, "net"):
            name = _getval(net, "name", "")
            code = _getval(net, "code", "")
            nodes = []
            for node in _getall(net, "node"):
                nodes.append(
                    {
                        "ref": _getval(node, "ref", ""),
                        "pin": str(_getval(node, "pin", "")),
                        "type": str(_getval(node, "pintype", "")),
                    }
                )
            nets.append({"name": name, "code": code, "nodes": nodes})

    return Netlist(components=components, nets=nets, pin_types=pin_types)
