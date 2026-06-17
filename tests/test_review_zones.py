"""Add a copper-zone OUTLINE (guarded). Pure parsing/building + a hermetic guard (kicad-cli
mocked) run anywhere; the real kicad-cli load-check is gated on a KiCad install + the board.
"""

import os
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sexpdata  # noqa: E402

from kicad_mcp.edit import zones  # noqa: E402
from kicad_mcp.edit.locate import EditError  # noqa: E402
from kicad_mcp.review.kicad import Project  # noqa: E402

_BOARD = os.environ.get(
    "KICAD_REVIEW_TEST_PROJECT", r"C:/Users/jonny/Desktop/Trellis/In-Pipe-Hardware/v0/PERIPH"
)


def _have_cli():
    from kicad_mcp.review import kicad

    try:
        kicad.find_kicad_cli()
        return True
    except kicad.KiCadError:
        return False


requires_board = pytest.mark.skipif(
    not Path(_BOARD).exists() or not _have_cli(), reason="needs kicad-cli + real board"
)

# a tiny board with a net table; the pad's (zone_connect 0) is a "(zone" substring trap.
_PCB = (
    "(kicad_pcb\n"
    '  (net 0 "")\n'
    '  (net 1 "GND")\n'
    '  (net 2 "VCC")\n'
    '  (footprint "R" (pad "1" smd (zone_connect 0)))\n'
    ")\n"
)


# --------------------------------------------------------------------------- #
# pure: net resolution, rectangle, zone construction
# --------------------------------------------------------------------------- #
def test_board_nets_and_resolve():
    assert zones.board_nets(_PCB) == {"": 0, "GND": 1, "VCC": 2}
    assert zones.resolve_net(_PCB, "GND") == 1
    assert zones.resolve_net(_PCB, "") == 0  # the no-net zone
    with pytest.raises(EditError):
        zones.resolve_net(_PCB, "NOPE")  # unknown net
    with pytest.raises(EditError):
        zones.resolve_net("(kicad_pcb)", "GND")  # no net table at all


def test_rect_points_normalizes():
    assert zones.rect_points(2, 3, 0, 1) == [(0, 1), (2, 1), (2, 3), (0, 3)]


def test_make_zone_and_insert():
    z = zones.make_zone(1, "GND", "B.Cu", [(0, 0), (1, 0), (1, 1), (0, 1)], 0.5, 0.25)
    assert "(net 1)" in z and '(net_name "GND")' in z and '(layer "B.Cu")' in z
    assert "(polygon (pts (xy 0 0) (xy 1 0)" in z
    assert z.count("(uuid") == 1
    out = zones.add_zone_text("(kicad_pcb\n  (foo)\n)\n", z)
    assert out.rstrip().endswith(")")
    sexpdata.loads(out)  # the assembled board re-parses


# --------------------------------------------------------------------------- #
# hermetic guard (kicad-cli mocked)
# --------------------------------------------------------------------------- #
def test_propose_zone_dry_run_then_apply(tmp_path, monkeypatch):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_PCB, encoding="utf-8")
    proj = Project(name="b", dir=tmp_path, pro=None, sch=None, pcb=pcb)
    monkeypatch.setattr(zones, "_loads_ok", lambda p: True)
    pts = zones.rect_points(0, 0, 10, 10)

    r = zones.propose_zone(proj, "GND", "B.Cu", pts, apply=False)
    assert r["loads_ok"] is True and r["applied"] is False
    assert r["net_num"] == 1
    assert '(net_name "GND")' in r["diff"] and "UNFILLED" in r["note"]
    assert pcb.read_text(encoding="utf-8") == _PCB  # dry run leaves the file untouched

    r2 = zones.propose_zone(proj, "GND", "B.Cu", pts, apply=True)
    assert r2["applied"] is True
    after = pcb.read_text(encoding="utf-8")
    assert '(net_name "GND")' in after and "(polygon (pts" in after
    sexpdata.loads(after)  # still parses


def test_propose_zone_rejects_bad_inputs(tmp_path, monkeypatch):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_PCB, encoding="utf-8")
    proj = Project(name="b", dir=tmp_path, pro=None, sch=None, pcb=pcb)
    monkeypatch.setattr(zones, "_loads_ok", lambda p: True)
    with pytest.raises(EditError):
        zones.propose_zone(proj, "GND", "F.SilkS", zones.rect_points(0, 0, 1, 1))  # not copper
    with pytest.raises(EditError):
        zones.propose_zone(proj, "GND", "B.Cu", [(0, 0), (1, 1)])  # < 3 points


# --------------------------------------------------------------------------- #
# integration: real board (never the live one -- a copy)
# --------------------------------------------------------------------------- #
@requires_board
def test_add_zone_on_real_board(tmp_path):
    from kicad_mcp.review import kicad

    dst = tmp_path / "PERIPH"
    shutil.copytree(
        _BOARD, dst, ignore=shutil.ignore_patterns(".kicad-review", "*-backups", "_autosave*", "~*")
    )
    proj = kicad.discover_project(dst)
    # net 0 needs no net table; a small rectangle inside the board is enough for the load check
    r = zones.propose_zone(proj, "", "B.Cu", zones.rect_points(150, 95, 160, 105), apply=True)
    assert r["loads_ok"] is True and r["applied"] is True
    assert "(xy 150 95)" in Path(proj.pcb).read_text(encoding="utf-8")
