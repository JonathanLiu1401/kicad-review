"""Engine orchestration + the three surfaces (engine API / CLI / MCP).

Self-contained: adds the plugin root to sys.path, gates KiCad-dependent tests on a
real board + kicad-cli. Confirmed bugs are encoded as strict xfails so the suite is
green and the bugs become an executable punch-list.
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.review import kicad  # noqa: E402
from kicad_mcp.review.engine import ReviewEngine  # noqa: E402

_BOARD = os.environ.get(
    "KICAD_REVIEW_TEST_PROJECT", r"C:/Users/jonny/Desktop/Trellis/In-Pipe-Hardware/v0/PERIPH"
)


def _have_cli():
    try:
        kicad.find_kicad_cli()
        return True
    except kicad.KiCadError:
        return False


requires_board = pytest.mark.skipif(
    not Path(_BOARD).exists() or not _have_cli(), reason="needs kicad-cli + real board"
)


def _load_cli_module():
    """Import lib/kicad_review_cli.py by path (it is a script, not a package module)."""
    path = ROOT / "lib" / "kicad_review_cli.py"
    spec = importlib.util.spec_from_file_location("kicad_review_cli", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- #
# scope validation (no KiCad needed beyond constructing the engine)
# --------------------------------------------------------------------------- #
@requires_board
def test_review_rejects_unknown_scope():
    eng = ReviewEngine(_BOARD)
    with pytest.raises(kicad.KiCadError):
        eng.review(scope="bogus", render=False)


@requires_board
def test_review_accepts_known_scopes():
    # 'schematic' scope runs ERC/netlist only; should complete and return a dict
    eng = ReviewEngine(_BOARD)
    pkg = eng.review(scope="schematic", render=False)
    assert isinstance(pkg, dict)
    assert pkg["meta"]["scope"] == "schematic"


# --------------------------------------------------------------------------- #
# full-review shape + content
# --------------------------------------------------------------------------- #
@requires_board
def test_review_shape_and_content():
    eng = ReviewEngine(_BOARD)
    pkg = eng.review(scope="all", render=False)
    for key in (
        "meta",
        "findings",
        "images",
        "datasheets",
        "rubric",
        "report_markdown",
        "report_markdown_path",
        "report_json_path",
    ):
        assert key in pkg, key
    assert pkg["meta"]["kicad_version"].startswith(("9", "10"))
    assert pkg["findings"], "PERIPH must produce findings"
    # no stage crashed
    assert not [f for f in pkg["findings"] if f["check"] == "engine"]
    checks_seen = {f["check"] for f in pkg["findings"]}
    assert "net_classes" in checks_seen
    assert "drc_parity" in checks_seen
    assert "erc" in checks_seen


@requires_board
def test_review_no_render_yields_no_images():
    eng = ReviewEngine(_BOARD)
    pkg = eng.review(scope="all", render=False)
    assert pkg["images"] == []


# --------------------------------------------------------------------------- #
# _stage degradation: a failing runner must NOT crash the review
# --------------------------------------------------------------------------- #
@requires_board
def test_stage_failure_degrades_gracefully(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("simulated ERC failure")

    monkeypatch.setattr(kicad, "run_erc", _boom)
    eng = ReviewEngine(_BOARD)
    pkg = eng.review(scope="all", render=False)  # must not raise
    assert isinstance(pkg, dict)
    stage_fails = [f for f in pkg["findings"] if f["check"] == "engine"]
    assert stage_fails, "a failing stage should surface an INFO finding, not crash"
    assert any(f["severity"] == "info" for f in stage_fails)


# --------------------------------------------------------------------------- #
# _parse_currents on both surfaces (CLI string form vs MCP dict form)
# --------------------------------------------------------------------------- #
def test_cli_parse_currents():
    cli = _load_cli_module()
    assert cli._parse_currents(["12V=4.0", "5V=1.5"]) == {"12V": 4.0, "5V": 1.5}
    # bad inputs dropped, no crash
    assert cli._parse_currents(["nope", "x=abc", "a=b=c", "n="]) == {}
    assert cli._parse_currents([]) == {}


def test_mcp_parse_currents():
    pytest.importorskip("fastmcp")
    from kicad_mcp.tools.review_tools import _parse_currents

    assert _parse_currents({"12V": 4.0, "VM": "3.5"}) == {"12V": 4.0, "VM": 3.5}
    assert _parse_currents({"bad": "xyz"}) == {}
    assert _parse_currents(None) == {}


# --------------------------------------------------------------------------- #
# MCP tool registration
# --------------------------------------------------------------------------- #
def test_mcp_tools_register():
    pytest.importorskip("fastmcp")
    from fastmcp import FastMCP

    from kicad_mcp.tools.review_tools import register_review_tools

    mcp = FastMCP("test")
    register_review_tools(mcp)  # must not raise

    import asyncio

    tools = asyncio.run(mcp.get_tools())
    names = set(tools.keys()) if isinstance(tools, dict) else {t.name for t in tools}
    for expected in ("kicad_review", "kicad_inspect", "kicad_erc", "kicad_drc", "kicad_render"):
        assert expected in names, expected


# --------------------------------------------------------------------------- #
# CONFIRMED BUGS (strict xfail until fixed)
# --------------------------------------------------------------------------- #
@requires_board
@pytest.mark.xfail(
    reason="bug #2: engine._find_datasheets is called outside _stage, so an OSError "
    "while globbing parent dirs crashes the already-finished review",
    strict=True,
)
def test_datasheet_discovery_crash_does_not_discard_review(monkeypatch):
    def _boom(self):
        raise OSError("simulated permission error while globbing datasheets")

    monkeypatch.setattr(ReviewEngine, "_find_datasheets", _boom)
    eng = ReviewEngine(_BOARD)
    # CORRECT behavior: the review survives a datasheet-discovery failure (like other
    # stages). CURRENT: review() raises OSError, so this assertion is never reached.
    assert isinstance(eng.review(scope="all", render=False), dict)


@pytest.mark.xfail(
    reason="bug #6: MCP _safe catches only KiCadError, so TimeoutExpired/JSONDecodeError "
    "escape the tool instead of becoming a structured {'error': ...}",
    strict=True,
)
def test_mcp_safe_catches_non_kicad_errors():
    pytest.importorskip("fastmcp")
    from kicad_mcp.tools.review_tools import _safe

    @_safe
    def f():
        raise subprocess.TimeoutExpired(cmd="kicad-cli", timeout=1)

    result = f()
    # CORRECT: a structured error dict. CURRENT: TimeoutExpired propagates uncaught.
    assert isinstance(result, dict) and "error" in result
