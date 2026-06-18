"""JLCPCB manufacturability check + guarded design-rule apply.

Capability lookups, the .kicad_pro rule reader, and the guarded apply are pure/hermetic; the
full board check is gated on a KiCad install + the board.
"""

import json
import os
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.edit.board_rules import propose_jlcpcb_rules  # noqa: E402
from kicad_mcp.edit.board_stackup import propose_stackup  # noqa: E402
from kicad_mcp.edit.locate import EditError  # noqa: E402
from kicad_mcp.review import jlcpcb  # noqa: E402
from kicad_mcp.review.kicad import Project  # noqa: E402

_OLD_BOARD = Path(
    r"C:/Users/jonny/Desktop/Trellis/driver-bms-datasheets/bms-driver-layout-old/PERIPH.kicad_pcb"
)

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

# a .kicad_pro whose rules are looser than JLCPCB, with a SECOND min_clearance (0.5) in another
# block -- the apply must edit only the rules-block one.
_PRO = (
    '{ "board": { "design_settings": {'
    '  "teardrop_options": { "min_clearance": 0.5 },'
    '  "rules": {'
    '    "min_clearance": 0.16,'
    '    "min_copper_edge_clearance": 0.2,'
    '    "min_through_hole_diameter": 0.1,'
    '    "min_track_width": 0.2,'
    '    "min_via_diameter": 0.6,'
    '    "min_via_annular_width": 0.15'
    "  } } } }"
)


# --------------------------------------------------------------------------- #
# pure: capability lookups
# --------------------------------------------------------------------------- #
def test_capabilities_layer_tiers():
    assert jlcpcb.capabilities_for(2, 1)["min_track_width"] == 0.127  # 1-2 layer
    assert jlcpcb.capabilities_for(4, 1)["min_track_width"] == 0.09  # 4+ layer tighter
    assert jlcpcb.capabilities_for(2, 1)["min_via_drill"] == 0.30
    assert jlcpcb.capabilities_for(4, 1)["min_via_drill"] == 0.20


def test_capabilities_2oz_widens_track():
    assert jlcpcb.capabilities_for(4, 2)["min_track_width"] == 0.20  # 2 oz needs wider tracks


def test_board_rules_reads_and_missing(tmp_path):
    pro = tmp_path / "x.kicad_pro"
    pro.write_text(_PRO, encoding="utf-8")
    assert jlcpcb.board_rules(pro)["min_copper_edge_clearance"] == 0.2
    assert jlcpcb.board_rules(None) == {}


# --------------------------------------------------------------------------- #
# guarded apply (hermetic: no PCB -> 1-2 layer limits)
# --------------------------------------------------------------------------- #
def test_apply_rules_anchored_and_round_trips(tmp_path):
    pro = tmp_path / "x.kicad_pro"
    pro.write_text(_PRO, encoding="utf-8")
    proj = Project(name="x", dir=tmp_path, pro=pro, sch=None, pcb=None)

    r = propose_jlcpcb_rules(proj, apply=False)
    ch = {c["rule"]: (c["old"], c["new"]) for c in r["changes"]}
    assert ch["min_copper_edge_clearance"] == (0.2, 0.3)
    assert ch["min_through_hole_diameter"] == (0.1, 0.15)
    assert "min_clearance" not in ch  # 0.16 already meets the JLCPCB floor -> not raised
    assert r["applied"] is False
    # dry run leaves the file untouched
    assert (
        json.loads(pro.read_text())["board"]["design_settings"]["rules"][
            "min_copper_edge_clearance"
        ]
        == 0.2
    )

    r2 = propose_jlcpcb_rules(proj, apply=True)
    assert r2["applied"] is True
    ds = json.loads(pro.read_text())["board"]["design_settings"]
    assert ds["rules"]["min_copper_edge_clearance"] == 0.3
    assert ds["rules"]["min_through_hole_diameter"] == 0.15
    assert ds["teardrop_options"]["min_clearance"] == 0.5  # the OTHER min_clearance untouched


def test_apply_rules_noop_when_already_strict(tmp_path):
    strict = json.dumps(
        {
            "board": {
                "design_settings": {
                    "rules": {"min_copper_edge_clearance": 0.5, "min_through_hole_diameter": 0.3}
                }
            }
        }
    )
    pro = tmp_path / "x.kicad_pro"
    pro.write_text(strict, encoding="utf-8")
    proj = Project(name="x", dir=tmp_path, pro=pro, sch=None, pcb=None)
    r = propose_jlcpcb_rules(proj, apply=True)
    assert r["changes"] == [] and r["applied"] is False


# --------------------------------------------------------------------------- #
# stackup: reference lookup, parsing, mismatch
# --------------------------------------------------------------------------- #
_STACKUP_PCB = (
    "(kicad_pcb\n  (setup\n    (stackup\n"
    '      (layer "F.Cu" (type "copper") (thickness 0.035))\n'
    '      (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (epsilon_r 4.4))\n'
    '      (layer "In1.Cu" (type "copper") (thickness 0.0152))\n'
    '      (layer "dielectric 2" (type "core") (thickness 1.065) (epsilon_r 4.43))\n'
    '      (layer "In2.Cu" (type "copper") (thickness 0.0152))\n'
    '      (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (epsilon_r 4.4))\n'
    '      (layer "B.Cu" (type "copper") (thickness 0.035))\n'
    "    )\n  )\n)\n"
)


def test_reference_stackup_lookup():
    assert jlcpcb.reference_stackup(4, 1.6)["code"] == "JLC04161H-7628"
    assert jlcpcb.reference_stackup(2, 1.6) is None  # 2-layer: no impedance stackup
    assert jlcpcb.reference_stackup(4, 2.0) is None  # not vendored -> None (caller flags the gap)


def test_parse_board_stackup(tmp_path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text(_STACKUP_PCB, encoding="utf-8")
    layers = jlcpcb.parse_board_stackup(pcb)
    assert [layer["type"] for layer in layers] == [
        "copper",
        "prepreg",
        "copper",
        "core",
        "copper",
        "prepreg",
        "copper",
    ]
    assert jlcpcb.parse_board_stackup(tmp_path / "missing.kicad_pcb") == []


def test_stackup_matches_jlcpcb_reference(tmp_path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text(_STACKUP_PCB, encoding="utf-8")
    layers = jlcpcb.parse_board_stackup(pcb)
    assert jlcpcb._stackup_dielectric_mismatch(layers, jlcpcb.STACKUPS[(4, 1.6)]) == []


def test_stackup_mismatch_detected(tmp_path):
    pcb = tmp_path / "x.kicad_pcb"
    pcb.write_text(_STACKUP_PCB.replace("0.2104", "0.1"), encoding="utf-8")  # generic prepreg
    layers = jlcpcb.parse_board_stackup(pcb)
    mismatch = jlcpcb._stackup_dielectric_mismatch(layers, jlcpcb.STACKUPS[(4, 1.6)])
    assert mismatch and any("prepreg" in m for m in mismatch)


# --------------------------------------------------------------------------- #
# stackup WRITE (hermetic: parse_board + kicad-cli mocked)
# --------------------------------------------------------------------------- #
_STACKUP_GENERIC = (  # KiCad's generic 4-layer default -- NOT JLCPCB's stack
    "(kicad_pcb\n  (setup\n    (stackup\n"
    '      (layer "F.Cu" (type "copper") (thickness 0.035))\n'
    '      (layer "dielectric 1" (type "prepreg") (thickness 0.1) (epsilon_r 4.5))\n'
    '      (layer "In1.Cu" (type "copper") (thickness 0.035))\n'
    '      (layer "dielectric 2" (type "core") (thickness 1.24) (epsilon_r 4.5))\n'
    '      (layer "In2.Cu" (type "copper") (thickness 0.035))\n'
    '      (layer "dielectric 3" (type "prepreg") (thickness 0.1) (epsilon_r 4.5))\n'
    '      (layer "B.Cu" (type "copper") (thickness 0.035))\n'
    "    )\n  )\n)\n"
)


def _fake_4layer(monkeypatch):
    from kicad_mcp.edit import board_stackup

    class _B:
        copper_layers = 4
        copper_oz = 1.0

    monkeypatch.setattr(board_stackup, "parse_board", lambda p: _B())
    monkeypatch.setattr(board_stackup.jlcpcb, "board_thickness_mm", lambda p: 1.6)
    monkeypatch.setattr(board_stackup, "_loads_ok", lambda p: True)


def test_propose_stackup_updates_to_jlcpcb(tmp_path, monkeypatch):
    _fake_4layer(monkeypatch)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_STACKUP_GENERIC, encoding="utf-8")
    proj = Project(name="b", dir=tmp_path, pro=None, sch=None, pcb=pcb)

    r = propose_stackup(proj, apply=False)
    assert r["code"] == "JLC04161H-7628"
    assert len(r["changes"]) == 8  # 3 dielectric thickness + 2 epsilon_r + ... + 2 inner copper
    assert pcb.read_text(encoding="utf-8") == _STACKUP_GENERIC  # dry run untouched

    r2 = propose_stackup(proj, apply=True)
    assert r2["applied"] is True
    di = [
        layer for layer in jlcpcb.parse_board_stackup(pcb) if layer["type"] in ("prepreg", "core")
    ]
    assert [layer["thickness"] for layer in di] == [0.2104, 1.065, 0.2104]
    assert [layer["epsilon_r"] for layer in di] == [4.4, 4.43, 4.4]


def test_propose_stackup_noop_when_already_correct(tmp_path, monkeypatch):
    _fake_4layer(monkeypatch)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_STACKUP_PCB, encoding="utf-8")  # already the JLCPCB stack
    proj = Project(name="b", dir=tmp_path, pro=None, sch=None, pcb=pcb)
    r = propose_stackup(proj, apply=True)
    assert r["changes"] == [] and r["applied"] is False


def test_propose_stackup_no_stackup_block_raises(tmp_path, monkeypatch):
    _fake_4layer(monkeypatch)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb\n  (setup)\n)\n", encoding="utf-8")
    proj = Project(name="b", dir=tmp_path, pro=None, sch=None, pcb=pcb)
    with pytest.raises(EditError):
        propose_stackup(proj)


@pytest.mark.skipif(
    not _OLD_BOARD.exists() or not _have_cli(), reason="needs the stable old board + kicad-cli"
)
def test_propose_stackup_on_stable_old_board(tmp_path):
    from kicad_mcp.review import kicad

    dst = tmp_path / "PERIPH.kicad_pcb"
    shutil.copy(_OLD_BOARD, dst)
    proj = kicad.discover_project(dst)
    r = propose_stackup(proj, apply=False)  # the old board's generic stack differs from JLCPCB
    assert r["loads_ok"] is True
    assert r["changes"] and r["code"] == "JLC04161H-7628"


# --------------------------------------------------------------------------- #
# integration: real board
# --------------------------------------------------------------------------- #
@requires_board
def test_check_on_real_board():
    from kicad_mcp.review import kicad

    proj = kicad.discover_project(_BOARD)
    r = jlcpcb.check_jlcpcb_manufacturability(proj)
    assert r["layers"] >= 1 and r["copper_oz"] > 0
    assert isinstance(r["manufacturable"], bool)
    assert r["sources"] and r["verified"]
    # at least one finding has a cited, structured shape
    assert all({"severity", "title", "detail"} <= set(f) for f in r["findings"])
