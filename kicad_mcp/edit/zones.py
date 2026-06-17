"""Add a copper-zone (pour) OUTLINE to a ``.kicad_pcb`` -- a deterministic, fully-specified
board edit (explicit net + layer + polygon).

IMPORTANT: kicad-cli cannot FILL zones (verified -- it plots only cached fills), so this writes
an UNFILLED outline. The user fills it in KiCad (Edit > Fill All Zones / `B`). This is the line
between "basic, specifiable board ops" (here) and routing/placement (advice-only -- see SKILL.md).
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

import sexpdata

from kicad_mcp.edit.locate import EditError
from kicad_mcp.review import kicad

_NET_RE = re.compile(r'\(net (\d+) "((?:[^"\\]|\\.)*)"\)')
_LAYER_RE = re.compile(r"(F|B|In\d+)\.Cu")


def board_nets(pcb_text: str) -> dict[str, int]:
    """{net_name: net_number} from the .kicad_pcb numbered net table."""
    return {m.group(2): int(m.group(1)) for m in _NET_RE.finditer(pcb_text)}


def resolve_net(pcb_text: str, net_name: str) -> int:
    """The net number for ``net_name`` (or 0 for the unconnected net ""). Raises with a clear
    message when the board has no net table or the net is unknown."""
    if net_name == "":
        return 0
    nets = board_nets(pcb_text)
    if not nets:
        raise EditError(
            "this .kicad_pcb has no numbered net table (name-only or unassigned nets) -- assign "
            "nets in the schematic and update the PCB (F8) before adding a zone"
        )
    if net_name not in nets:
        sample = ", ".join(sorted(nets)[:8])
        raise EditError(f"net {net_name!r} not found on the board (have: {sample}, ...)")
    return nets[net_name]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def rect_points(x1: float, y1: float, x2: float, y2: float) -> list[tuple[float, float]]:
    """Rectangle corners (mm), normalized so (x1,y1) is the min corner."""
    lo_x, hi_x = sorted((x1, x2))
    lo_y, hi_y = sorted((y1, y2))
    return [(lo_x, lo_y), (hi_x, lo_y), (hi_x, hi_y), (lo_x, hi_y)]


def make_zone(
    net_num: int,
    net_name: str,
    layer: str,
    points: list[tuple[float, float]],
    clearance: float,
    min_thickness: float,
) -> str:
    """The ``(zone ...)`` S-expression block for an unfilled pour outline (pure)."""
    pts = " ".join(f"(xy {x} {y})" for x, y in points)
    return (
        f"\t(zone\n"
        f"\t\t(net {net_num})\n"
        f'\t\t(net_name "{_esc(net_name)}")\n'
        f'\t\t(layer "{layer}")\n'
        f'\t\t(uuid "{uuid.uuid4()}")\n'
        f"\t\t(hatch edge 0.508)\n"
        f"\t\t(connect_pads (clearance {clearance}))\n"
        f"\t\t(min_thickness {min_thickness})\n"
        f"\t\t(filled_areas_thickness no)\n"
        f"\t\t(fill (thermal_gap 0.508) (thermal_bridge_width 0.508))\n"
        f"\t\t(polygon (pts {pts}))\n"
        f"\t)\n"
    )


def add_zone_text(pcb_text: str, zone_block: str) -> str:
    """Insert ``zone_block`` just before the final ``)`` of the kicad_pcb (pure)."""
    cut = pcb_text.rstrip().rfind(")")
    if cut < 0:
        raise EditError("not a valid .kicad_pcb (no closing parenthesis)")
    return pcb_text[:cut] + zone_block + ")\n"


def _loads_ok(pcb_path: Path) -> bool:
    """True if kicad-cli loads the board (DRC runs without a load failure)."""
    proj = kicad.Project(name=pcb_path.stem, dir=pcb_path.parent, pro=None, sch=None, pcb=pcb_path)
    try:
        kicad.run_drc(proj, parity=False)
        return True
    except kicad.KiCadError:
        return False


def propose_zone(
    project,
    net_name: str,
    layer: str,
    points: list[tuple[float, float]],
    clearance: float = 0.5,
    min_thickness: float = 0.25,
    apply: bool = False,
) -> dict:
    """Propose (or apply) adding a copper-zone OUTLINE. Guarded: the edited board must still load
    in kicad-cli. The zone is UNFILLED -- the user fills it in KiCad. Returns a dict with diff,
    loads_ok, applied, and a note.
    """
    if not project.pcb:
        raise EditError("project has no .kicad_pcb to add a zone to")
    if not _LAYER_RE.fullmatch(layer):
        raise EditError(f"layer {layer!r} is not a copper layer (use F.Cu, B.Cu, In1.Cu, ...)")
    if len(points) < 3:
        raise EditError("a zone polygon needs at least 3 points")

    orig = Path(project.pcb).read_text(encoding="utf-8")
    net_num = resolve_net(orig, net_name)
    zone = make_zone(net_num, net_name, layer, points, clearance, min_thickness)
    new_text = add_zone_text(orig, zone)
    sexpdata.loads(new_text)  # structural gate: still parses

    tmp_dir = Path(tempfile.mkdtemp(prefix="kicad-zone-"))
    try:
        tmp_pcb = tmp_dir / (Path(project.pcb).stem + ".kicad_pcb")
        tmp_pcb.write_text(new_text, encoding="utf-8")
        loads_ok = _loads_ok(tmp_pcb)

        diff = "".join(
            difflib.unified_diff(
                orig.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile="before",
                tofile=f"+ zone on {net_name or '<no net>'}/{layer}",
            )
        )
        applied = False
        if apply and loads_ok:
            tmp = Path(project.pcb).with_name(Path(project.pcb).name + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            os.replace(tmp, project.pcb)  # atomic
            applied = True

        return {
            "net": net_name,
            "net_num": net_num,
            "layer": layer,
            "points": points,
            "diff": diff,
            "loads_ok": loads_ok,
            "applied": applied,
            "note": "the zone is UNFILLED -- open the board in KiCad and Edit > Fill All Zones (B); "
            "kicad-cli cannot fill zones headlessly",
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
