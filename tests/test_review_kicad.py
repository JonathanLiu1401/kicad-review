"""Self-contained tests for ``kicad_mcp.review.kicad``.

Three tiers:
  * No-KiCad unit tests (always run): kicad-cli locator numeric version sort,
    KICAD_CLI_PATH override, project discovery on synthetic tmp files, and the
    ``workdir`` helper. These never shell out.
  * @requires_cli: the confirmed BLOCKER stale-artifact bug -- ``run_erc`` reads a
    prior run's report instead of raising when kicad-cli fails. Marked xfail
    (strict) so this file goes RED the day the bug is fixed.
  * @requires_board: the headless runners end to end against a real board
    (ERC/DRC/netlist/BOM/PDF/3D). Skipped automatically when kicad-cli or the
    board is absent, so the suite stays portable.

Point the board tests at any project dir with::

    set KICAD_REVIEW_TEST_PROJECT=C:\\path\\to\\project_dir
"""

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.review import kicad  # noqa: E402

_BOARD = os.environ.get(
    "KICAD_REVIEW_TEST_PROJECT", r"C:/Users/jonny/Desktop/Trellis/In-Pipe-Hardware/v0/PERIPH"
)


def _have_cli():
    try:
        kicad.find_kicad_cli()
        return True
    except kicad.KiCadError:
        return False


requires_cli = pytest.mark.skipif(not _have_cli(), reason="needs kicad-cli")
requires_board = pytest.mark.skipif(
    not Path(_BOARD).exists() or not _have_cli(), reason="needs kicad-cli + real board"
)


# --------------------------------------------------------------------------- #
# kicad-cli location (no KiCad needed)
# --------------------------------------------------------------------------- #
def test_candidate_cli_paths_numeric_version_sort(monkeypatch):
    """Regression: the old lexical sort put "9.0" > "10.0" and picked KiCad 9.

    Force the Windows branch and feed an out-of-order version listing; the 10.0
    path must rank ahead of the 9.0 and 8.0 paths.
    """
    monkeypatch.setattr(kicad.platform, "system", lambda: "Windows")
    monkeypatch.setattr(kicad.os, "listdir", lambda _root: ["9.0", "10.0", "8.0"])

    cands = kicad._candidate_cli_paths()

    assert cands, "expected candidate paths on the Windows branch"
    assert any("10.0" in c for c in cands)

    def first_idx(token):
        return next(i for i, c in enumerate(cands) if token in c)

    assert first_idx("10.0") < first_idx("9.0"), cands
    assert first_idx("10.0") < first_idx("8.0"), cands
    # every emitted path is the kicad-cli executable
    assert all(c.endswith("kicad-cli.exe") for c in cands), cands


def test_candidate_cli_paths_darwin(monkeypatch):
    """The macOS branch returns the KiCad.app bundle path."""
    monkeypatch.setattr(kicad.platform, "system", lambda: "Darwin")

    cands = kicad._candidate_cli_paths()

    assert cands
    assert all(c.endswith("/kicad-cli") for c in cands), cands
    assert any("KiCad.app" in c for c in cands), cands


def test_candidate_cli_paths_linux(monkeypatch):
    """The Linux branch returns the usual /usr[/local]/bin locations."""
    monkeypatch.setattr(kicad.platform, "system", lambda: "Linux")

    cands = kicad._candidate_cli_paths()

    assert "/usr/bin/kicad-cli" in cands
    assert "/usr/local/bin/kicad-cli" in cands


def test_find_kicad_cli_not_found_raises(monkeypatch):
    """With no env override, nothing on PATH, and no install dirs -> KiCadError."""
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setattr(kicad.platform, "system", lambda: "Linux")
    monkeypatch.setattr(kicad.shutil, "which", lambda _name: None)
    monkeypatch.setattr(kicad, "_candidate_cli_paths", lambda: ["/no/such/kicad-cli"])

    with pytest.raises(kicad.KiCadError):
        kicad.find_kicad_cli()


def test_find_kicad_cli_from_path(monkeypatch, tmp_path):
    """When KICAD_CLI_PATH is unset, a hit on PATH (shutil.which) is returned."""
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    found = tmp_path / "kicad-cli"
    found.write_text("", encoding="utf-8")
    monkeypatch.setattr(kicad.shutil, "which", lambda _name: str(found))

    assert kicad.find_kicad_cli() == str(found)


def test_find_kicad_cli_env_override(monkeypatch, tmp_path):
    """KICAD_CLI_PATH that points at a real file wins over PATH/install dirs."""
    fake = tmp_path / "kicad-cli-fake.exe"
    fake.write_text("not a real binary", encoding="utf-8")
    monkeypatch.setenv("KICAD_CLI_PATH", str(fake))

    assert kicad.find_kicad_cli() == str(fake)


def test_find_kicad_cli_env_override_ignored_when_missing(monkeypatch, tmp_path):
    """A KICAD_CLI_PATH pointing at a nonexistent file is ignored (falls through)."""
    monkeypatch.setenv("KICAD_CLI_PATH", str(tmp_path / "does-not-exist.exe"))
    # PATH lookup and install dirs find nothing -> KiCadError on machines w/o KiCad,
    # but on a machine WITH KiCad it returns the real one. Either way it must NOT
    # echo back the bogus override.
    try:
        result = kicad.find_kicad_cli()
    except kicad.KiCadError:
        return
    assert result != str(tmp_path / "does-not-exist.exe")


# --------------------------------------------------------------------------- #
# project discovery (no KiCad needed -- synthetic empty files)
# --------------------------------------------------------------------------- #
def _touch(path: Path) -> Path:
    path.write_text("", encoding="utf-8")
    return path


def test_discover_project_full_bundle(tmp_path):
    """name.kicad_pro + .kicad_sch + .kicad_pcb -> all three resolve."""
    _touch(tmp_path / "name.kicad_pro")
    _touch(tmp_path / "name.kicad_sch")
    _touch(tmp_path / "name.kicad_pcb")

    proj = kicad.discover_project(tmp_path)

    assert proj.name == "name"
    assert proj.dir == tmp_path.resolve()
    assert proj.pro is not None and proj.pro.name == "name.kicad_pro"
    assert proj.sch is not None and proj.sch.name == "name.kicad_sch"
    assert proj.pcb is not None and proj.pcb.name == "name.kicad_pcb"
    assert proj.exists()


def test_discover_project_from_sch_file(tmp_path):
    """A .kicad_sch file argument resolves the whole bundle by stem."""
    _touch(tmp_path / "name.kicad_pro")
    sch = _touch(tmp_path / "name.kicad_sch")
    _touch(tmp_path / "name.kicad_pcb")

    proj = kicad.discover_project(sch)

    assert proj.sch == sch.resolve()
    assert proj.pcb is not None and proj.pcb.name == "name.kicad_pcb"
    assert proj.pro is not None and proj.pro.name == "name.kicad_pro"


def test_discover_project_from_pcb_file(tmp_path):
    """A .kicad_pcb file argument also resolves the bundle."""
    pcb = _touch(tmp_path / "name.kicad_pcb")
    _touch(tmp_path / "name.kicad_sch")

    proj = kicad.discover_project(pcb)

    assert proj.pcb == pcb.resolve()
    assert proj.sch is not None and proj.sch.name == "name.kicad_sch"
    assert proj.name == "name"


def test_discover_project_nonexistent_raises(tmp_path):
    with pytest.raises(kicad.KiCadError):
        kicad.discover_project(tmp_path / "nope")


def test_discover_project_empty_dir_raises(tmp_path):
    """A directory with no .kicad_sch / .kicad_pcb raises KiCadError."""
    _touch(tmp_path / "readme.txt")
    with pytest.raises(kicad.KiCadError):
        kicad.discover_project(tmp_path)


def test_discover_project_excludes_autosave_and_backup(tmp_path):
    """Autosave/backup siblings must never be chosen as the canonical files.

    With a clean ``name.*`` present alongside ``_autosave-name.kicad_sch`` and
    ``~name.kicad_pcb``, the picked files are the clean ones.
    """
    _touch(tmp_path / "name.kicad_pro")
    _touch(tmp_path / "name.kicad_sch")
    _touch(tmp_path / "name.kicad_pcb")
    _touch(tmp_path / "_autosave-name.kicad_sch")
    _touch(tmp_path / "~name.kicad_pcb")

    proj = kicad.discover_project(tmp_path)

    assert proj.sch is not None and proj.sch.name == "name.kicad_sch"
    assert proj.pcb is not None and proj.pcb.name == "name.kicad_pcb"
    assert "_autosave" not in proj.sch.name
    assert not proj.pcb.name.startswith("~")


def test_discover_project_excludes_autosave_without_pro(tmp_path):
    """Regression bite: no .kicad_pro, and the autosave sorts FIRST lexically.

    ``sorted(glob("*.kicad_sch"))`` puts ``_autosave-name.kicad_sch`` before
    ``name.kicad_sch`` (``_`` < ``n``). Without the exclusion filter the stem
    would be taken from the autosave; with it, the clean file wins.
    """
    _touch(tmp_path / "name.kicad_sch")
    _touch(tmp_path / "_autosave-name.kicad_sch")

    proj = kicad.discover_project(tmp_path)

    assert proj.name == "name"
    assert proj.sch is not None and proj.sch.name == "name.kicad_sch"
    assert "_autosave" not in proj.sch.name


@pytest.mark.xfail(
    reason="discover_project silently picks pros[0] for ambiguous dirs",
    strict=False,
)
def test_discover_project_ambiguous_two_projects(tmp_path):
    """A dir with TWO complete projects SHOULD flag ambiguity, but currently
    just sorts and grabs ``pros[0]`` (alpha) with no warning."""
    _touch(tmp_path / "alpha.kicad_pro")
    _touch(tmp_path / "alpha.kicad_sch")
    _touch(tmp_path / "alpha.kicad_pcb")
    _touch(tmp_path / "zebra.kicad_pro")
    _touch(tmp_path / "zebra.kicad_sch")
    _touch(tmp_path / "zebra.kicad_pcb")

    # Correct behaviour: refuse to guess between two projects.
    with pytest.raises(kicad.KiCadError):
        kicad.discover_project(tmp_path)


def test_workdir_creates_review_dir(tmp_path):
    """workdir() defaults to <project>/.kicad-review and creates it."""
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)

    wd = kicad.workdir(proj)

    assert wd == (tmp_path / ".kicad-review").resolve()
    assert wd.is_dir()


def test_workdir_honors_out_override(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    out = tmp_path / "custom-out"

    wd = kicad.workdir(proj, out=str(out))

    assert wd == out.resolve()
    assert wd.is_dir()


# --------------------------------------------------------------------------- #
# runner guards (no KiCad needed -- they raise before shelling out)
# --------------------------------------------------------------------------- #
def test_run_erc_requires_schematic(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.run_erc(proj, out=str(tmp_path))


def test_run_drc_requires_pcb(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.run_drc(proj, out=str(tmp_path))


def test_export_netlist_requires_schematic(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.export_netlist(proj, out=str(tmp_path))


def test_export_bom_requires_schematic(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.export_bom(proj, out=str(tmp_path))


def test_render_schematic_pdf_requires_schematic(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.render_schematic_pdf(proj, out=str(tmp_path))


def test_render_board_pdf_requires_pcb(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.render_board_pdf(proj, out=str(tmp_path))


def test_render_3d_requires_pcb(tmp_path):
    proj = kicad.Project(name="p", dir=tmp_path, pro=None, sch=None, pcb=None)
    with pytest.raises(kicad.KiCadError):
        kicad.render_3d(proj, out=str(tmp_path))


# --------------------------------------------------------------------------- #
# CONFIRMED BLOCKER BUG: stale-artifact read
# --------------------------------------------------------------------------- #
@requires_cli
@pytest.mark.xfail(
    reason="BLOCKER: stale-artifact read -- runner returns a prior run's file when kicad-cli fails",
    strict=True,
)
def test_run_erc_raises_on_cli_failure_does_not_read_stale(tmp_path):
    """A broken schematic makes kicad-cli exit non-zero WITHOUT overwriting the
    pre-existing ``erc.json``.

    CORRECT behaviour: ``run_erc`` notices the failed run (returncode != 0) and
    raises ``KiCadError``.

    CURRENT behaviour: it only checks ``dest.is_file()``, finds the pre-seeded
    stale report, and returns ``{"violations": [], "STALE": True}`` -- so the
    ``pytest.raises`` below is NOT satisfied and the test XFAILS (strict).
    """
    broken = tmp_path / "broken.kicad_sch"
    broken.write_text("(kicad_sch garbage", encoding="utf-8")

    project = kicad.Project(name="broken", dir=tmp_path, pro=None, sch=broken, pcb=None)

    # Pre-seed the exact destination workdir/erc.json that run_erc will read.
    seed_dir = kicad.workdir(project, out=str(tmp_path))
    seed = seed_dir / "erc.json"
    seed.write_text('{"violations": [], "STALE": true}', encoding="utf-8")

    with pytest.raises(kicad.KiCadError):
        kicad.run_erc(project, out=str(tmp_path))


# --------------------------------------------------------------------------- #
# kicad-cli version
# --------------------------------------------------------------------------- #
@requires_cli
def test_cli_version_returns_string():
    v = kicad.cli_version()
    assert isinstance(v, str)
    assert v
    assert v.startswith("10"), v


# --------------------------------------------------------------------------- #
# integration: headless runners against the real board
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def project():
    return kicad.discover_project(_BOARD)


@requires_board
def test_discover_real_board(project):
    """The real PERIPH dir resolves all three files and excludes its autosave."""
    assert project.sch is not None and project.sch.is_file()
    assert project.pcb is not None and project.pcb.is_file()
    assert "_autosave" not in project.sch.name
    assert not project.pcb.name.startswith("~")


@requires_board
def test_run_erc_real(project, tmp_path):
    res = kicad.run_erc(project, out=str(tmp_path))
    assert isinstance(res, dict)
    # KiCad 10 emits per-sheet violations under "sheets" plus top-level
    # "ignored_checks"; older formats may put a flat "violations" list.
    assert "violations" in res or "sheets" in res, sorted(res)
    assert "ignored_checks" in res, sorted(res)
    assert (tmp_path / "erc.json").is_file()


@requires_board
def test_run_drc_real(project, tmp_path):
    res = kicad.run_drc(project, out=str(tmp_path))
    assert isinstance(res, dict)
    assert "violations" in res, sorted(res)
    assert "schematic_parity" in res, sorted(res)
    assert "unconnected_items" in res, sorted(res)
    assert (tmp_path / "drc.json").is_file()


@requires_board
def test_export_netlist_real(project, tmp_path):
    dest = kicad.export_netlist(project, out=str(tmp_path))
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert dest.suffix == ".net"


@requires_board
def test_export_bom_real(project, tmp_path):
    dest = kicad.export_bom(project, out=str(tmp_path))
    assert dest.is_file()
    assert dest.stat().st_size > 0


@requires_board
def test_render_schematic_pdf_real(project, tmp_path):
    dest = kicad.render_schematic_pdf(project, out=str(tmp_path))
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert dest.suffix == ".pdf"


@requires_board
@pytest.mark.parametrize("preset", ["front", "back", "copper"])
def test_render_board_pdf_presets_real(project, tmp_path, preset):
    dest = kicad.render_board_pdf(project, preset=preset, out=str(tmp_path))
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert preset in dest.name


@requires_board
def test_render_3d_real(project, tmp_path):
    dest = kicad.render_3d(project, out=str(tmp_path))
    assert dest.is_file()
    assert dest.stat().st_size > 0
    assert dest.suffix == ".png"
