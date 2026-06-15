"""Golden tests for the kicad-review engine.

Two tiers:
  * Unit tests (always run): IPC-2221 math + net classification — no KiCad needed.
  * Integration tests (need kicad-cli + a real board): run the engine end-to-end
    and assert it surfaces the *known* PERIPH findings. Skipped automatically if
    kicad-cli or the board is absent, so the suite is portable.

Point the integration tests at any board with::

    set KICAD_REVIEW_TEST_PROJECT=C:\\path\\to\\project_dir

Defaults to the in-repo PERIPH board on the original author's machine.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# make the plugin package importable regardless of CWD
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.review import checks  # noqa: E402
from kicad_mcp.review.checks import (  # noqa: E402
    ipc2221_capacity_a, ipc2221_width_mm, is_ground, is_power,
)

DEFAULT_BOARD = r"C:/Users/jonny/Desktop/Trellis/In-Pipe-Hardware/v0/PERIPH"


# --------------------------------------------------------------------------- #
# unit: IPC-2221 trace-current model
# --------------------------------------------------------------------------- #
def test_ipc2221_known_point():
    # 0.5 mm, 1 oz external, 10 °C rise ≈ 1.2..1.7 A (textbook ballpark)
    cap = ipc2221_capacity_a(0.5, dT_c=10.0, copper_oz=1.0, external=True)
    assert 1.1 < cap < 1.8, cap


def test_ipc2221_roundtrip():
    cap = ipc2221_capacity_a(0.5, 10.0, 1.0, True)
    w = ipc2221_width_mm(cap, 10.0, 1.0, True)
    assert abs(w - 0.5) < 0.02, w


def test_ipc2221_monotonic():
    assert (ipc2221_capacity_a(1.0) > ipc2221_capacity_a(0.5) > ipc2221_capacity_a(0.16))
    # internal layers carry less than external for the same width
    assert ipc2221_capacity_a(0.5, external=False) < ipc2221_capacity_a(0.5, external=True)


def test_ipc2221_zero():
    assert ipc2221_capacity_a(0) == 0.0
    assert ipc2221_width_mm(0) == 0.0


# --------------------------------------------------------------------------- #
# unit: net-name classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["GND", "GNDREF", "AGND", "0", "GND2"])
def test_is_ground(name):
    assert is_ground(name)


@pytest.mark.parametrize("name", ["12V", "+12V", "3.3V", "5V", "VBAT", "Battery In",
                                  "VM", "Motor 1 Out 1", "VIN", "VOUT",
                                  "RAW_5V", "SYS_3V3", "BUS-12V"])
def test_is_power(name):
    assert is_power(name)


@pytest.mark.parametrize("name", ["SCL", "SDA", "FAULT_N", "SLEEP_N", "INT", "RESET",
                                  "SWDIO", "VREF", "VSENSE"])
def test_signal_not_power(name):
    assert not is_power(name)


# --------------------------------------------------------------------------- #
# unit: trace-current checks on synthetic boards (no KiCad needed)
# --------------------------------------------------------------------------- #
def _board(tracks, poured=None):
    from kicad_mcp.review.parse import Board
    nets = {1: "12V"}
    return Board(nets=nets, tracks=tracks, vias=[], footprints=[],
                 copper_layers=2, copper_oz=1.0, poured_nets=poured or set())


def test_trace_ignores_short_fanout_stub():
    """A fat main run in parallel with a short thin stub must NOT be flagged."""
    from kicad_mcp.review.parse import Track
    tracks = [
        Track(width=0.5, layer="F.Cu", net=1, length=40.0),   # main run
        Track(width=0.16, layer="F.Cu", net=1, length=0.4),   # pad fanout stub
    ]
    out = checks.check_trace_currents(_board(tracks), current_specs={"12V": 1.0})
    majors = [f for f in out if f.severity.value == "major"]
    assert not majors, majors  # 0.5 mm carries ~1.45 A > 1.0 A


def test_trace_flags_sustained_thin_run():
    """A genuinely thin sustained run IS flagged."""
    from kicad_mcp.review.parse import Track
    tracks = [Track(width=0.16, layer="F.Cu", net=1, length=20.0)]
    out = checks.check_trace_currents(_board(tracks), current_specs={"12V": 4.0})
    assert any(f.severity.value == "major" for f in out)


def test_trace_pin_type_power_net_included():
    """A net not matching the name heuristic is still sized if passed via power_nets."""
    from kicad_mcp.review.parse import Board, Track
    b = Board(nets={1: "Net-(U4-REGOUT)"}, tracks=[Track(0.16, "F.Cu", 1, 20.0)],
              vias=[], footprints=[], copper_layers=2, copper_oz=1.0)
    # without the hint: not recognized as power -> no finding
    assert not checks.check_trace_currents(b, {"NET-(U4-REGOUT)": 3.0})
    # with the pin-type hint: recognized -> flagged
    out = checks.check_trace_currents(b, {"NET-(U4-REGOUT)": 3.0},
                                      power_nets={"Net-(U4-REGOUT)"})
    assert any(f.severity.value == "major" for f in out)


# --------------------------------------------------------------------------- #
# integration: the real board
# --------------------------------------------------------------------------- #
def test_cli_locator_prefers_newest_version(monkeypatch):
    """Regression: lexical sort picked KiCad 9 over 10. Must prefer the newest."""
    import platform
    from kicad_mcp.review import kicad
    if platform.system() != "Windows":
        pytest.skip("Windows-specific install-path test")
    root = Path(r"C:/Program Files/KiCad")
    versions = [d.name for d in root.glob("*") if d.is_dir()] if root.exists() else []
    if "9.0" not in versions or "10.0" not in versions:
        pytest.skip("needs both KiCad 9.0 and 10.0 installed")
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    cands = kicad._candidate_cli_paths()
    first_with_both = next((c for c in cands if "10.0" in c or "9.0" in c), "")
    assert "10.0" in first_with_both, f"picked {first_with_both} (should be 10.0)"


def _board_path() -> str | None:
    p = os.environ.get("KICAD_REVIEW_TEST_PROJECT", DEFAULT_BOARD)
    return p if Path(p).exists() else None


def _have_cli() -> bool:
    from kicad_mcp.review import kicad
    try:
        kicad.find_kicad_cli()
        return True
    except kicad.KiCadError:
        return False


pytestmark_board = pytest.mark.skipif(
    _board_path() is None or not _have_cli(),
    reason="needs kicad-cli + a real board (set KICAD_REVIEW_TEST_PROJECT)",
)


@pytest.fixture(scope="module")
def review_pkg():
    from kicad_mcp.review import ReviewEngine
    eng = ReviewEngine(_board_path())
    return eng.review(scope="all", render=False)


@pytestmark_board
def test_engine_runs_clean(review_pkg):
    # no stage crashed (engine failures are tagged check == "engine")
    fails = [f for f in review_pkg["findings"] if f["check"] == "engine"]
    assert not fails, fails
    assert review_pkg["meta"]["kicad_version"].startswith(("9", "10"))


@pytestmark_board
def test_finds_single_net_class(review_pkg):
    assert any(f["check"] == "net_classes" for f in review_pkg["findings"])


@pytestmark_board
def test_finds_schematic_parity(review_pkg):
    parity = [f for f in review_pkg["findings"] if f["check"] == "drc_parity"]
    assert parity and parity[0]["severity"] == "major"


@pytestmark_board
def test_finds_power_pin_not_driven(review_pkg):
    erc = [f for f in review_pkg["findings"]
           if f["check"] == "erc" and "power_pin_not_driven" in f["title"]]
    assert erc


@pytestmark_board
def test_finds_disabled_erc_checks(review_pkg):
    assert any(f["check"] == "erc_suppressions" for f in review_pkg["findings"])


@pytestmark_board
def test_finds_thin_power_or_decoupling(review_pkg):
    # the board has thin power nets and/or far decoupling caps — at least one fires
    pt = [f for f in review_pkg["findings"]
          if f["check"] in ("trace_currents", "decoupling")]
    assert pt


@pytestmark_board
def test_report_files_written(review_pkg):
    assert Path(review_pkg["report_markdown_path"]).is_file()
    assert Path(review_pkg["report_json_path"]).is_file()


# --- copper-pour awareness (regression: don't flag poured nets off track stubs) --- #
@pytestmark_board
def test_board_detects_pours():
    from kicad_mcp.review import kicad
    from kicad_mcp.review.parse import parse_board
    proj = kicad.discover_project(_board_path())
    board = parse_board(proj.pcb)
    # PERIPH pours 3.3V (In1.Cu) and GND (In2.Cu)
    assert "GND" in board.poured_nets
    assert "3.3V" in board.poured_nets


@pytestmark_board
def test_poured_net_not_hard_flagged():
    """A poured power net asked for a high current must be SOFTENED (minor), never a
    hard 'undersized' major, because its bulk current goes through the pour."""
    from kicad_mcp.review import kicad
    from kicad_mcp.review.parse import parse_board
    proj = kicad.discover_project(_board_path())
    board = parse_board(proj.pcb)
    found = checks.check_trace_currents(board, current_specs={"3.3V": 5.0})
    threes = [f for f in found if f.location.get("net") == "3.3V"]
    # if 3.3V surfaces at all, it must be minor (poured), not a major false positive
    assert all(f.severity.value != "major" for f in threes), threes
