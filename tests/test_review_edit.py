"""Surgical schematic-editing engine: locate / surgical / guard.

Pure-text tests run anywhere; the real-board tests are gated on a KiCad install + the
PERIPH copy and never touch the live board (they work on a tmp copy).
"""

import difflib
import os
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sexpdata  # noqa: E402

from kicad_mcp.edit import find_instance, set_value  # noqa: E402
from kicad_mcp.edit.locate import EditError, list_references  # noqa: E402
from kicad_mcp.edit.surgical import edit_property_text  # noqa: E402

_BOARD = os.environ.get(
    "KICAD_REVIEW_TEST_PROJECT", r"C:/Users/jonny/Desktop/Trellis/In-Pipe-Hardware/v0/PERIPH"
)
_UUID = "11111111-2222-3333-4444-555555555555"
_MINI = (
    "(kicad_sch\n"
    "\t(lib_symbols\n"
    '\t\t(symbol "Device:R"\n'
    '\t\t\t(property "Value" "R")\n'
    "\t\t)\n"
    "\t)\n"
    "\t(symbol\n"
    '\t\t(lib_id "Device:R")\n'
    "\t\t(at 10 20 0)\n"
    f'\t\t(uuid "{_UUID}")\n'
    '\t\t(property "Reference" "R1")\n'
    '\t\t(property "Value" "10k")\n'
    '\t\t(property "Footprint" "Resistor_SMD:R_0603")\n'
    "\t)\n"
    ")\n"
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


# --------------------------------------------------------------------------- #
# pure: edit_property_text (no KiCad)
# --------------------------------------------------------------------------- #
def test_edit_property_text_replaces_only_the_target_instance():
    out = edit_property_text(_MINI, _UUID, "Value", "4.7k")
    assert '(property "Value" "4.7k")' in out
    # the lib_symbols cache value "R" (earlier in the file) is untouched
    assert '(property "Value" "R")' in out
    changed = [
        line
        for line in difflib.ndiff(_MINI.splitlines(), out.splitlines())
        if line.startswith(("+ ", "- "))
    ]
    assert len(changed) == 2  # exactly one line removed + one added


def test_edit_property_text_footprint():
    out = edit_property_text(_MINI, _UUID, "Footprint", "Capacitor_SMD:C_0805")
    assert '(property "Footprint" "Capacitor_SMD:C_0805")' in out


def test_edit_property_text_unknown_uuid_raises():
    with pytest.raises(EditError):
        edit_property_text(_MINI, "deadbeef-0000", "Value", "x")


def test_edit_property_text_unknown_property_raises():
    with pytest.raises(EditError):
        edit_property_text(_MINI, _UUID, "Nope", "x")


def test_edit_property_text_escapes_quotes_and_backslash():
    out = edit_property_text(_MINI, _UUID, "Value", 'a"b\\c')
    assert r'(property "Value" "a\"b\\c")' in out


# --------------------------------------------------------------------------- #
# locate / set_value on a written mini-sch
# --------------------------------------------------------------------------- #
def test_find_instance_fields(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_MINI, encoding="utf-8")
    inst = find_instance(sch, "R1")
    assert inst is not None
    assert inst.reference == "R1"
    assert inst.uuid == _UUID
    assert inst.value == "10k"
    assert inst.footprint == "Resistor_SMD:R_0603"
    assert inst.lib_id == "Device:R"
    assert find_instance(sch, "NOPE") is None


def test_list_references(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_MINI, encoding="utf-8")
    assert list_references(sch) == ["R1"]


def test_set_value_roundtrip(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_MINI, encoding="utf-8")
    old = set_value(sch, "R1", "4.7k")
    assert old == "10k"
    assert find_instance(sch, "R1").value == "4.7k"
    sexpdata.loads(sch.read_text(encoding="utf-8"))  # still parses


def test_set_value_missing_ref_raises(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_MINI, encoding="utf-8")
    with pytest.raises(EditError):
        set_value(sch, "R99", "1k")


# --------------------------------------------------------------------------- #
# integration: real PERIPH copy (never the live board)
# --------------------------------------------------------------------------- #
@requires_board
def test_surgical_edit_on_real_board_is_byte_clean(tmp_path):
    src = Path(_BOARD) / "PERIPH.kicad_sch"
    sch = tmp_path / "PERIPH.kicad_sch"
    shutil.copy(src, sch)
    ref = next(r for r in list_references(sch) if r.startswith("C"))
    before = sch.read_text(encoding="utf-8")
    set_value(sch, ref, "12.34uF")
    after = sch.read_text(encoding="utf-8")
    assert find_instance(sch, ref).value == "12.34uF"
    changed = [
        line
        for line in difflib.ndiff(before.splitlines(), after.splitlines())
        if line.startswith(("+ ", "- "))
    ]
    assert len(changed) == 2  # only the one Value line
    sexpdata.loads(after)  # still parses


@requires_board
def test_guard_dry_run_then_apply(tmp_path):
    from kicad_mcp.edit.guard import propose_edit
    from kicad_mcp.review import kicad

    dst = tmp_path / "PERIPH"
    shutil.copytree(
        _BOARD, dst, ignore=shutil.ignore_patterns(".kicad-review", "*-backups", "_autosave*", "~*")
    )
    proj = kicad.discover_project(dst)
    ref = next(r for r in list_references(proj.sch) if r.startswith("C"))
    live_before = Path(proj.sch).read_text(encoding="utf-8")

    # dry run: diff produced, no ERC regression, live file UNCHANGED
    res = propose_edit(proj, ref, "Value", "99uF", apply=False)
    assert res["applied"] is False
    assert "99uF" in res["diff"]
    assert not res["erc_regressed"]
    assert Path(proj.sch).read_text(encoding="utf-8") == live_before

    # apply: live file now carries the new value
    res2 = propose_edit(proj, ref, "Value", "99uF", apply=True)
    assert res2["applied"] is True
    assert find_instance(proj.sch, ref).value == "99uF"
