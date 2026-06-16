"""Comprehensive, self-contained unit tests for ``kicad_mcp.review.checks``.

Everything here is synthetic: ``Board`` / ``Netlist`` / ``ProjectSettings`` are
constructed by hand, so no KiCad install, no real ``.kicad_pcb`` / ``.net`` files
and no ``kicad-cli`` are needed. The goal is *non-vacuous* coverage: every
"should fire" assertion is paired with proof the producing path is live, and
every "should not fire" assertion is paired with a positive control on the same
machinery so a green result cannot come from a mis-wired input.

Four confirmed bugs are pinned with ``@pytest.mark.xfail(..., strict=True)``:
the test encodes the *correct* behaviour, so it fails on today's code (xfail) and
will flip to a hard failure (xpass) the moment the bug is fixed.
"""

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.review import checks  # noqa: E402
from kicad_mcp.review.parse import (  # noqa: E402
    Board,
    Footprint,
    Netlist,
    ProjectSettings,
    Track,
)
from kicad_mcp.review.report import Domain, Severity  # noqa: E402

# IPC-2221 constants, redeclared here so the formula assertions are independent
# of the module's private names (an accidental edit to the module's constants
# should make these tests fail, not silently agree).
_MM_PER_MIL = 0.0254
_MIL_PER_OZ = 1.378  # 1 oz finished copper thickness, in mils
_K_EXT = 0.048
_K_INT = 0.024


def _ipc_capacity(width_mm: float, dT_c: float = 10.0, oz: float = 1.0, external: bool = True):
    """Reference re-implementation of I = k·dT^0.44·A^0.725 for cross-checking."""
    if width_mm <= 0:
        return 0.0
    k = _K_EXT if external else _K_INT
    width_mils = width_mm / _MM_PER_MIL
    area_mils2 = width_mils * (oz * _MIL_PER_OZ)
    return k * (dT_c**0.44) * (area_mils2**0.725)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _board(
    nets: dict | None = None,
    tracks: list | None = None,
    *,
    poured: set | None = None,
    footprints: list | None = None,
    copper_oz: float = 1.0,
    copper_layers: int = 2,
) -> Board:
    return Board(
        nets=nets if nets is not None else {1: "12V"},
        tracks=tracks or [],
        vias=[],
        footprints=footprints or [],
        copper_layers=copper_layers,
        copper_oz=copper_oz,
        poured_nets=poured or set(),
    )


def _netlist(nets: list | None = None, components: list | None = None) -> Netlist:
    return Netlist(components=components or [], nets=nets or [], pin_types={})


def _net(name: str, *nodes: tuple, code: str = "1") -> dict:
    """``_net("3.3V", ("U1", "power_in"), ("C1", "passive"))`` -> netlist net dict."""
    return {
        "name": name,
        "code": code,
        "nodes": [{"ref": r, "pin": "1", "type": t} for r, t in nodes],
    }


def _majors(findings: list) -> list:
    return [f for f in findings if f.severity is Severity.MAJOR]


def _ids(findings: list) -> list[str]:
    return [f.id for f in findings]


# =========================================================================== #
# IPC-2221 trace-current model
# =========================================================================== #
@pytest.mark.parametrize("width_mm", [0.1, 0.16, 0.25, 0.5, 0.8, 1.0, 2.0])
def test_ipc2221_matches_formula(width_mm):
    """Module output must equal the independent reference formula at several widths."""
    got = checks.ipc2221_capacity_a(width_mm, dT_c=10.0, copper_oz=1.0, external=True)
    expected = _ipc_capacity(width_mm, 10.0, 1.0, True)
    assert got == pytest.approx(expected, rel=1e-9), (width_mm, got, expected)
    # and on internal layers (k halved)
    got_int = checks.ipc2221_capacity_a(width_mm, dT_c=10.0, copper_oz=1.0, external=False)
    assert got_int == pytest.approx(_ipc_capacity(width_mm, 10.0, 1.0, False), rel=1e-9)


def test_ipc2221_internal_is_half_of_external():
    """k_int / k_ext = 0.024 / 0.048 = 0.5 exactly, for any width."""
    for w in (0.16, 0.3, 0.5, 1.0):
        ext = checks.ipc2221_capacity_a(w, external=True)
        internal = checks.ipc2221_capacity_a(w, external=False)
        assert internal == pytest.approx(0.5 * ext, rel=1e-9)
        assert internal < ext


@pytest.mark.parametrize("current_a", [0.5, 1.0, 1.45, 3.0, 5.0])
def test_ipc2221_width_capacity_are_inverses(current_a):
    """width_mm(capacity_a(w)) == w and capacity_a(width_mm(I)) == I."""
    w = checks.ipc2221_width_mm(current_a, dT_c=10.0, copper_oz=1.0, external=True)
    back = checks.ipc2221_capacity_a(w, dT_c=10.0, copper_oz=1.0, external=True)
    assert back == pytest.approx(current_a, rel=1e-6), (current_a, w, back)
    # and the other direction at a fixed width
    cap = checks.ipc2221_capacity_a(0.5, external=True)
    w2 = checks.ipc2221_width_mm(cap, external=True)
    assert w2 == pytest.approx(0.5, rel=1e-6)


def test_ipc2221_monotonic_in_width_and_current():
    caps = [checks.ipc2221_capacity_a(w) for w in (0.1, 0.16, 0.25, 0.5, 1.0, 2.0)]
    assert caps == sorted(caps)
    assert len(set(caps)) == len(caps)  # strictly increasing
    widths = [checks.ipc2221_width_mm(i) for i in (0.5, 1.0, 2.0, 4.0)]
    assert widths == sorted(widths)


def test_ipc2221_known_textbook_ballpark():
    # 0.5 mm, 1 oz, external, 10 C rise lands in the classic 1.2..1.7 A band.
    cap = checks.ipc2221_capacity_a(0.5, dT_c=10.0, copper_oz=1.0, external=True)
    assert 1.1 < cap < 1.8, cap


def test_ipc2221_hand_checked_literal_value():
    """Pin the 0.5 mm external capacity to an independently hand-computed number.

    I = 0.048 · 10^0.44 · (0.5/0.0254 · 1.378)^0.725 ≈ 1.447 A. Hard-coding the value
    (not re-deriving it) catches a shared sign/constant slip the formula-mirror test
    above could not.
    """
    assert checks.ipc2221_capacity_a(0.5, external=True) == pytest.approx(1.447, abs=0.005)
    # internal layer at the same width is exactly half.
    assert checks.ipc2221_capacity_a(0.5, external=False) == pytest.approx(0.723, abs=0.005)


def test_ipc2221_zero_and_negative_clamp_to_zero():
    assert checks.ipc2221_capacity_a(0) == 0.0
    assert checks.ipc2221_capacity_a(-0.5) == 0.0
    assert checks.ipc2221_width_mm(0) == 0.0
    assert checks.ipc2221_width_mm(-1.0) == 0.0


def test_ipc2221_copper_weight_scales_capacity():
    """2 oz copper carries more than 1 oz at the same width (more cross-section)."""
    assert checks.ipc2221_capacity_a(0.5, copper_oz=2.0) > checks.ipc2221_capacity_a(
        0.5, copper_oz=1.0
    )


# =========================================================================== #
# net-name classification
# =========================================================================== #
_POWER_NAMES = [
    "12V",
    "+12V",
    "3.3V",
    "5V",
    "VBAT",
    "Battery In",
    "VM",
    "Motor 1 Out 1",
    "VIN",
    "VOUT",
    "RAW_5V",
    "SYS_3V3",
    "BUS-12V",
]
_GROUND_NAMES = ["GND", "GNDREF", "AGND", "0", "PGND"]
_NOT_POWER_NAMES = ["SCL", "SDA", "FAULT_N", "SWDIO", "VREF", "VSENSE", "RESET"]


@pytest.mark.parametrize("name", _POWER_NAMES)
def test_is_power_true(name):
    assert checks.is_power(name) is True


@pytest.mark.parametrize("name", _GROUND_NAMES)
def test_is_ground_true(name):
    assert checks.is_ground(name) is True
    # ground is always power, too (is_power short-circuits on is_ground)
    assert checks.is_power(name) is True


@pytest.mark.parametrize("name", _NOT_POWER_NAMES)
def test_not_power_and_not_ground(name):
    assert checks.is_power(name) is False, name
    assert checks.is_ground(name) is False, name


def test_classification_handles_empty_and_none():
    assert checks.is_power("") is False
    assert checks.is_ground("") is False
    assert checks.is_power(None) is False
    assert checks.is_ground(None) is False


# =========================================================================== #
# check_trace_currents  (positive controls everywhere)
# =========================================================================== #
def test_trace_sustained_thin_run_is_majored():
    """POSITIVE CONTROL: a sustained 0.16 mm run asked for 4 A IS a MAJOR finding."""
    board = _board(tracks=[Track(width=0.16, layer="F.Cu", net=1, length=20.0)])
    out = checks.check_trace_currents(board, current_specs={"12V": 4.0})
    majors = _majors(out)
    assert majors, out  # the finding must EXIST
    assert any(f.id == "trace-12V" and f.check == "trace_currents" for f in majors)
    assert majors[0].domain is Domain.POWER_THERMAL


def test_trace_fat_main_with_short_stub_not_majored_but_other_net_fires():
    """A fat 0.5 mm main run + a short 0.16 mm fanout stub on 12V must NOT major.

    Non-vacuity: a *second* power net (5V) routed at a thin sustained width with no
    spec produces an informational finding, so the call returns >0 findings overall
    and we can assert specifically that 12V is not majored (not just "no majors").
    """
    nets = {1: "12V", 2: "5V"}
    tracks = [
        Track(width=0.5, layer="F.Cu", net=1, length=40.0),  # 12V main run (~1.45 A)
        Track(width=0.16, layer="F.Cu", net=1, length=0.4),  # 12V pad-fanout stub
        Track(width=0.16, layer="F.Cu", net=2, length=20.0),  # 5V thin sustained run
    ]
    out = checks.check_trace_currents(_board(nets, tracks), current_specs={"12V": 1.0})
    assert out, "expected the thin 5V net to surface an informational finding"
    # 12V is the fat run -> never a major; the stub must not drag it down.
    twelve = [f for f in out if f.location.get("net") == "12V"]
    assert not any(f.severity is Severity.MAJOR for f in twelve), twelve
    # and the live finding really is the 5V informational capacity one.
    assert any(f.id == "trace-cap-5V" for f in out), _ids(out)


def test_trace_inline_control_thin_main_flips_to_major():
    """Same shape as above but the *main* run is thin -> a major DOES fire.

    Proves the no-major result above came from geometry, not a dead code path.
    """
    tracks = [
        Track(width=0.16, layer="F.Cu", net=1, length=40.0),  # thin main run now
        Track(width=0.16, layer="F.Cu", net=1, length=0.4),  # stub
    ]
    out = checks.check_trace_currents(_board(tracks=tracks), current_specs={"12V": 1.0})
    assert any(f.id == "trace-12V" and f.severity is Severity.MAJOR for f in out), out


def test_trace_poured_net_softened_to_minor_not_major():
    """A poured power net with a high spec is softened to MINOR (not a MAJOR)."""
    board = _board(
        nets={1: "3.3V"},
        tracks=[Track(width=0.16, layer="F.Cu", net=1, length=20.0)],
        poured={"3.3V"},
    )
    out = checks.check_trace_currents(board, current_specs={"3.3V": 5.0})
    threes = [f for f in out if f.location.get("net") == "3.3V"]
    assert threes, "the poured net must still surface a finding before we check severity"
    assert all(f.severity is Severity.MINOR for f in threes), threes
    assert all(f.severity is not Severity.MAJOR for f in threes)
    # the softened finding explains the pour explicitly
    assert any("poured" in f.title.lower() for f in threes)


def test_trace_power_nets_param_includes_non_name_matching_net():
    """A net whose NAME is not a power pattern is still sized when passed via power_nets."""
    name = "Net-(U4-REGOUT)"
    board = _board(
        nets={1: name},
        tracks=[Track(width=0.16, layer="F.Cu", net=1, length=20.0)],
    )
    # without the hint -> not recognized as power -> no finding at all
    assert checks.check_trace_currents(board, {name.upper(): 3.0}) == []
    # with the pin-type hint -> recognized and flagged
    out = checks.check_trace_currents(board, {name.upper(): 3.0}, power_nets={name})
    assert any(f.severity is Severity.MAJOR and f.location.get("net") == name for f in out), out


def test_trace_ground_nets_are_skipped():
    board = _board(
        nets={1: "GND"},
        tracks=[Track(width=0.10, layer="F.Cu", net=1, length=30.0)],
    )
    # ground is handled separately; even an absurdly thin GND yields nothing here
    assert checks.check_trace_currents(board, current_specs={"GND": 10.0}) == []


def test_trace_informational_only_below_threshold_no_spec():
    """Without a spec, a thin (<=0.30 mm) power net gets an informational MINOR."""
    board = _board(tracks=[Track(width=0.20, layer="F.Cu", net=1, length=20.0)])
    out = checks.check_trace_currents(board)  # no current_specs
    assert any(f.id == "trace-cap-12V" and f.severity is Severity.MINOR for f in out), out


def test_trace_multiple_fat_no_spec_nets_all_quiet():
    """Two fat (>0.30 mm) power nets with no spec both stay quiet.

    Exercises the loop-continuation path where the informational threshold is not
    met for a *non-final* net (the 0.80 mm net falls through, then the loop proceeds
    to the 1.0 mm net), so neither yields a finding.
    """
    nets = {1: "12V", 2: "5V"}
    tracks = [
        Track(width=0.80, layer="F.Cu", net=1, length=20.0),
        Track(width=1.00, layer="F.Cu", net=2, length=20.0),
    ]
    assert checks.check_trace_currents(_board(nets, tracks)) == []


def test_trace_all_stub_net_uses_min_width_fallback():
    """If every track on a net is a short stub (<1 mm), fall back to the min width."""
    board = _board(
        tracks=[
            Track(width=0.16, layer="F.Cu", net=1, length=0.3),
            Track(width=0.40, layer="F.Cu", net=1, length=0.2),
        ]
    )
    out = checks.check_trace_currents(board, current_specs={"12V": 4.0})
    # fallback min width is 0.16 mm -> undersized for 4 A -> a major fires
    assert any(f.id == "trace-12V" and f.severity is Severity.MAJOR for f in out), out


def test_trace_ignores_negative_net_and_zero_width():
    board = _board(
        tracks=[
            Track(width=0.16, layer="F.Cu", net=-1, length=20.0),  # no net
            Track(width=0.0, layer="F.Cu", net=1, length=20.0),  # zero width
        ]
    )
    assert checks.check_trace_currents(board, current_specs={"12V": 4.0}) == []


def test_trace_empty_board_no_findings():
    assert checks.check_trace_currents(_board(tracks=[])) == []


# =========================================================================== #
# check_decoupling
# =========================================================================== #
def test_decoupling_missing_vs_present():
    nl = _netlist(
        nets=[
            _net("3.3V", ("U1", "power_in"), ("C1", "passive")),  # has a cap -> ok
            _net("5V", ("U2", "power_in")),  # no cap -> flagged
        ]
    )
    out = checks.check_decoupling(nl, None)
    assert any(f.id == "decap-missing-U2-5V" and f.severity is Severity.MAJOR for f in out)
    assert not any(f.id.startswith("decap-missing-U1") for f in out)
    assert all(f.check == "decoupling" for f in out)


def test_decoupling_ground_nets_ignored():
    """Even a U-pin power_in on GND is not treated as a power-input rail."""
    nl = _netlist(nets=[_net("GND", ("U1", "power_in"))])
    assert checks.check_decoupling(nl, None) == []


def test_decoupling_non_ic_power_in_ignored():
    """power_in pins on non-U refs (e.g. a connector J1) do not demand a decap."""
    nl = _netlist(nets=[_net("12V", ("J1", "power_in"))])
    assert checks.check_decoupling(nl, None) == []


def test_decoupling_cap_missing_board_position_skipped_in_distance():
    """A cap on the net but absent from board coords is skipped in the distance loop.

    C1 has no footprint position (so ``c in pos`` is False and it is skipped), while
    C2 is placed far away -> the nearest-cap distance is computed from C2 alone and a
    MINOR 'far' finding still fires. Exercises the inner-loop skip branch.
    """
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), ("C1", "passive"), ("C2", "passive"))])
    fps = [
        Footprint(ref="U1", value="", layer="F.Cu", x=0.0, y=0.0),
        Footprint(ref="C2", value="100nF", layer="F.Cu", x=20.0, y=0.0),  # far; C1 absent
    ]
    out = checks.check_decoupling(nl, _board(footprints=fps))
    assert any(f.id == "decap-far-U1-3.3V" for f in out), out


def test_decoupling_far_cap_is_minor():
    """A present-but-distant cap (>5 mm) yields a MINOR 'far' finding, not 'missing'."""
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), ("C1", "passive"))])
    fps = [
        Footprint(ref="U1", value="", layer="F.Cu", x=0.0, y=0.0),
        Footprint(ref="C1", value="100nF", layer="F.Cu", x=20.0, y=0.0),  # 20 mm away
    ]
    out = checks.check_decoupling(nl, _board(footprints=fps))
    assert any(f.id == "decap-far-U1-3.3V" and f.severity is Severity.MINOR for f in out), out
    assert not any(f.id.startswith("decap-missing") for f in out)


def test_decoupling_near_cap_silent():
    """A cap within a couple mm produces no finding (positive control for 'far')."""
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), ("C1", "passive"))])
    fps = [
        Footprint(ref="U1", value="", layer="F.Cu", x=0.0, y=0.0),
        Footprint(ref="C1", value="100nF", layer="F.Cu", x=1.0, y=0.0),  # 1 mm away
    ]
    assert checks.check_decoupling(nl, _board(footprints=fps)) == []


def test_decoupling_ic_not_on_board_skips_distance():
    """If the IC has a cap but no board position, the distance branch is skipped."""
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), ("C1", "passive"))])
    # board has C1 but NOT U1 -> ic not in pos -> no missing, no far
    fps = [Footprint(ref="C1", value="100nF", layer="F.Cu", x=99.0, y=0.0)]
    assert checks.check_decoupling(nl, _board(footprints=fps)) == []


# --------------------------------------------------------------------------- #
# CONFIRMED BUG 1: startswith("C") matches connectors / crystals
# --------------------------------------------------------------------------- #
def test_decoupling_real_cap_present_positive_control():
    """Control for bug 1: a genuine cap C1 on the IC's power net DOES suppress the
    'missing' finding -- proving the detector works and is not always-silent."""
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), ("C1", "passive"))])
    out = checks.check_decoupling(nl, None)
    assert not any(f.id.startswith("decap-missing") for f in out)


def test_decoupling_no_cap_at_all_positive_control():
    """Control for bug 1: with NO C* node, the IC is correctly flagged 'missing'."""
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), ("R1", "passive"))])
    out = checks.check_decoupling(nl, None)
    assert any(f.id == "decap-missing-U1-3.3V" for f in out), out


@pytest.mark.xfail(
    reason="bug: decoupling treats any ref starting with 'C' (CONN1/CR1) as a bypass cap",
    strict=True,
)
@pytest.mark.parametrize("nonsense_ref", ["CONN1", "CR1"])
def test_decoupling_connector_or_crystal_not_a_cap(nonsense_ref):
    """CORRECT: a connector (CONN1) / crystal (CR1) is NOT a decoupling cap, so an IC
    whose only non-IC node on its power net is one of those still needs a decap.

    CURRENT: ``ref.startswith("C")`` counts CONN1/CR1 as a cap and suppresses the
    finding, so this fails today and is pinned as a strict xfail.
    """
    nl = _netlist(nets=[_net("3.3V", ("U1", "power_in"), (nonsense_ref, "passive"))])
    out = checks.check_decoupling(nl, None)
    assert any(f.id == "decap-missing-U1-3.3V" for f in out), out


# --------------------------------------------------------------------------- #
# CONFIRMED BUG 2: decoupling judged per-NET, not per-IC-pin
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    reason="bug: one cap anywhere on a shared rail suppresses decap-missing for every "
    "IC on that rail, even ICs far from the only cap (per-net not per-pin)",
    strict=True,
)
def test_decoupling_shared_rail_far_ics_flagged():
    """One rail 3.3V with U1, U2, U3 (all power_in) and a single C1 next to U1.

    CORRECT: U2 and U3 sit far from the only cap, so each needs its own bypass cap
    -> a missing/insufficient finding for the far ICs.

    CURRENT: the single C1 satisfies the whole net, so NO ``decap-missing`` is
    emitted for U2/U3 (only MINOR ``decap-far`` notes). Non-vacuity: we first assert
    the call did return findings (the 'far' notes), then assert the *missing* finding
    that today's per-net logic fails to produce.
    """
    nl = _netlist(
        nets=[
            _net(
                "3.3V",
                ("U1", "power_in"),
                ("U2", "power_in"),
                ("U3", "power_in"),
                ("C1", "passive"),
            )
        ]
    )
    fps = [
        Footprint(ref="U1", value="", layer="F.Cu", x=0.0, y=0.0),
        Footprint(ref="U2", value="", layer="F.Cu", x=50.0, y=0.0),
        Footprint(ref="U3", value="", layer="F.Cu", x=100.0, y=0.0),
        Footprint(ref="C1", value="100nF", layer="F.Cu", x=1.0, y=0.0),  # only near U1
    ]
    out = checks.check_decoupling(nl, _board(footprints=fps))
    assert out, "expected at least the 'decap-far' findings (liveness)"
    # CORRECT behaviour: the ICs that have no nearby cap should be reported missing.
    assert any(
        f.id.startswith("decap-missing") and f.location.get("refdes") in {"U2", "U3"} for f in out
    ), _ids(out)


# =========================================================================== #
# check_erc
# =========================================================================== #
def test_check_erc_triage_severity_and_recommendation():
    erc = {
        "sheets": [
            {
                "violations": [
                    {
                        "type": "power_pin_not_driven",
                        "severity": "error",
                        "description": "Input Power pin not driven",
                        "items": [{"description": "U1 Pin 5"}],
                    },
                    {"type": "lib_symbol_issues", "severity": "warning", "description": "y"},
                ]
            }
        ]
    }
    out = checks.check_erc(erc)
    pwr = next(f for f in out if "power_pin_not_driven" in f.title)
    assert pwr.severity is Severity.MAJOR  # severity == error -> MAJOR
    assert pwr.id == "erc-power_pin_not_driven"
    assert "PWR_FLAG" in pwr.recommendation  # the sharper, type-specific message
    assert "U1 Pin 5" in pwr.detail  # first item location threaded into the detail
    lib = next(f for f in out if "lib_symbol_issues" in f.title)
    assert lib.severity is Severity.MINOR  # severity == warning -> MINOR
    assert all(f.check == "erc" and f.domain is Domain.ELECTRICAL for f in out)


def test_check_erc_top_level_violations_and_unknown_type():
    """Top-level (non-sheet) violations are collected; unknown types get the default rec."""
    erc = {"violations": [{"type": "weird_new_check", "severity": "warning", "description": "z"}]}
    out = checks.check_erc(erc)
    assert len(out) == 1
    assert out[0].id == "erc-weird_new_check"
    assert "ERC panel" in out[0].recommendation  # default recommendation branch


def test_check_erc_counts_occurrences():
    erc = {
        "violations": [
            {"type": "pin_to_pin", "severity": "error", "description": "a"},
            {"type": "pin_to_pin", "severity": "error", "description": "b"},
        ]
    }
    out = checks.check_erc(erc)
    assert "×2" in out[0].title
    assert "compatible driver" in out[0].recommendation  # pin_to_pin-specific message


def test_check_erc_empty_is_keyerror_safe():
    assert checks.check_erc({}) == []
    assert checks.check_erc({"sheets": None, "violations": None}) == []


# --------------------------------------------------------------------------- #
# CONFIRMED BUG 3 (ERC half): excluded violations re-reported
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    reason="bug: ERC violations flagged excluded=true (user-suppressed) are re-reported",
    strict=True,
)
def test_check_erc_excluded_violation_not_reported():
    """CORRECT: an ERC item the user explicitly excluded should produce 0 findings.
    CURRENT: ``check_erc`` ignores the ``excluded`` flag and re-reports it."""
    erc = {
        "violations": [
            {"type": "pin_to_pin", "severity": "warning", "description": "x", "excluded": True}
        ]
    }
    assert checks.check_erc(erc) == []


# =========================================================================== #
# check_drc
# =========================================================================== #
def test_check_drc_triage_unconnected_and_parity():
    drc = {
        "violations": [
            {"type": "solder_mask_bridge", "severity": "error", "description": "bridge"},
            {"type": "silk_over_copper", "severity": "warning", "description": "silk"},
        ],
        "unconnected_items": [{}, {}],
        "schematic_parity": [{"type": "missing_footprint"}, {"type": "net_conflict"}],
    }
    out = checks.check_drc(drc)
    bridge = next(f for f in out if f.id == "drc-solder_mask_bridge")
    assert bridge.severity is Severity.MAJOR and bridge.domain is Domain.DFM
    silk = next(f for f in out if f.id == "drc-silk_over_copper")
    assert silk.severity is Severity.MINOR
    unconn = next(f for f in out if f.id == "drc-unconnected")
    assert unconn.severity is Severity.BLOCKER and unconn.domain is Domain.ELECTRICAL
    assert "2 unconnected" in unconn.title
    parity = next(f for f in out if f.id == "drc-parity")
    assert parity.severity is Severity.MAJOR and parity.check == "drc_parity"
    assert "missing_footprint" in parity.detail and "net_conflict" in parity.detail


def test_check_drc_empty_is_keyerror_safe():
    assert checks.check_drc({}) == []
    assert checks.check_drc({"violations": None, "unconnected_items": None}) == []


# --------------------------------------------------------------------------- #
# CONFIRMED BUG 3 (DRC half): excluded violations re-reported
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    reason="bug: DRC violations flagged excluded=true (user-suppressed) are re-reported",
    strict=True,
)
def test_check_drc_excluded_violation_not_reported():
    """CORRECT: a clearance violation the user excluded should produce 0 findings.
    CURRENT: ``check_drc`` ignores ``excluded`` and re-reports it as a MAJOR."""
    drc = {"violations": [{"type": "clearance", "severity": "error", "excluded": True}]}
    assert checks.check_drc(drc) == []


# =========================================================================== #
# check_erc_suppressions
# =========================================================================== #
def _pro(*, net_classes=None, erc_severities=None) -> ProjectSettings:
    return ProjectSettings(
        net_classes=net_classes if net_classes is not None else [{"name": "Default"}],
        net_class_assignments={},
        design_rules={},
        erc_severities=erc_severities or {},
    )


def test_erc_suppressions_disabled_and_downgraded():
    erc = {"ignored_checks": [{"key": "four_way_junction", "description": "z"}]}
    pro = _pro(erc_severities={f"rule{i}": "warning" for i in range(6)})
    out = checks.check_erc_suppressions(pro, erc)
    disabled = next(f for f in out if f.id == "erc-ignored")
    assert disabled.severity is Severity.MINOR and disabled.domain is Domain.HYGIENE
    assert "four_way_junction" in disabled.detail
    downgraded = next(f for f in out if f.id == "erc-downgraded")
    assert downgraded.severity is Severity.NIT
    assert "6 ERC rules" in downgraded.title
    assert all(f.check == "erc_suppressions" for f in out)


def test_erc_suppressions_few_downgrades_below_threshold():
    """Fewer than 5 warning-downgraded rules -> no 'downgraded' finding."""
    pro = _pro(erc_severities={f"rule{i}": "warning" for i in range(4)})
    out = checks.check_erc_suppressions(pro, {})
    assert not any(f.id == "erc-downgraded" for f in out)


def test_erc_suppressions_none_pro_and_empty_erc():
    assert checks.check_erc_suppressions(None, {}) == []
    # ignored present but pro is None -> still reports the disabled checks
    out = checks.check_erc_suppressions(None, {"ignored_checks": [{"key": "k"}]})
    assert any(f.id == "erc-ignored" for f in out)


def test_erc_suppressions_keyerror_safe():
    assert checks.check_erc_suppressions(_pro(), {}) == []


# =========================================================================== #
# check_net_classes
# =========================================================================== #
def test_net_classes_single_default_flagged():
    pro = _pro(net_classes=[{"name": "Default", "track_width": 0.2}])
    out = checks.check_net_classes(pro)
    f = next(x for x in out if x.id == "netclass-single")
    assert f.severity is Severity.MAJOR and f.domain is Domain.POWER_THERMAL
    assert f.check == "net_classes"
    assert "0.2 mm" in f.detail  # track width threaded in


def test_net_classes_multiple_not_flagged():
    """Positive control: two+ net classes -> no single-class finding."""
    pro = _pro(net_classes=[{"name": "Default", "track_width": 0.2}, {"name": "Power"}])
    assert checks.check_net_classes(pro) == []


def test_net_classes_none_pro_returns_empty():
    assert checks.check_net_classes(None) == []


def test_net_classes_empty_list_still_flags():
    """No classes at all is also a single-class condition (uses {} fallback)."""
    pro = _pro(net_classes=[])
    out = checks.check_net_classes(pro)
    assert any(f.id == "netclass-single" for f in out)


# =========================================================================== #
# check_bom
# =========================================================================== #
@pytest.mark.parametrize("placeholder", ["", "~", "?", "x", "TBD", "dnp", "value", "N/A"])
def test_bom_flags_each_placeholder_value(placeholder):
    nl = _netlist(
        components=[
            {"ref": "R1", "value": placeholder, "footprint": ""},
            {"ref": "R2", "value": "10k", "footprint": "x"},  # real value, not flagged
        ]
    )
    out = checks.check_bom(nl)
    f = next(x for x in out if x.id == "bom-missing-value")
    assert f.severity is Severity.MINOR and f.domain is Domain.BOM and f.check == "bom"
    assert "R1" in f.detail
    assert "R2" not in f.detail  # the real-valued part is not listed


def test_bom_all_values_present_no_finding():
    nl = _netlist(components=[{"ref": "R1", "value": "10k", "footprint": "x"}])
    assert checks.check_bom(nl) == []


def test_bom_truncates_long_list_with_ellipsis():
    comps = [{"ref": f"R{i}", "value": "", "footprint": ""} for i in range(25)]
    out = checks.check_bom(_netlist(components=comps))
    f = next(x for x in out if x.id == "bom-missing-value")
    assert "25 parts" in f.title
    assert f.detail.rstrip().endswith("…")  # truncation marker after the first 20


def test_bom_missing_value_key_is_safe():
    """A component dict lacking a 'value' key is treated as a placeholder, not a crash."""
    out = checks.check_bom(_netlist(components=[{"ref": "R1"}]))
    assert any(f.id == "bom-missing-value" for f in out)


def test_bom_empty_netlist():
    assert checks.check_bom(_netlist(components=[])) == []


# =========================================================================== #
# CONFIRMED BUG 4: external=True overstates inner-layer ampacity
# =========================================================================== #
def test_inner_layer_xfail_numbers_are_in_the_discriminating_band():
    """Document the explicit IPC-2221 numbers that make the bug-4 xfail discriminating.

    For a 0.5 mm track at 1 oz, 10 C rise:
      * external (k=0.048): capacity ~= 1.447 A  -> needed width for 1.07 A ~= 0.330 mm
      * internal (k=0.024): capacity ~= 0.723 A  -> needed width for 1.07 A ~= 0.858 mm
    A spec of 1.07 A sits strictly between the internal and external capacities, so an
    inner-layer-aware check (k=0.024) flags 0.5 mm as undersized, while the current
    external-only check (k=0.048) does not.
    """
    w, spec = 0.5, 1.07
    cap_ext = checks.ipc2221_capacity_a(w, external=True)
    cap_int = checks.ipc2221_capacity_a(w, external=False)
    assert cap_int < spec < cap_ext, (cap_int, spec, cap_ext)
    need_ext = checks.ipc2221_width_mm(spec, external=True)
    need_int = checks.ipc2221_width_mm(spec, external=False)
    # external model: NOT undersized (0.5 >= need_ext * 0.95); internal model: undersized.
    assert not (w < need_ext * 0.95)
    assert w < need_int * 0.95


def test_inner_layer_overcurrent_positive_control():
    """Control for bug 4: a spec ABOVE the external capacity (2.0 A > ~1.45 A) flags the
    same 0.5 mm inner-layer track even with the current external model -- proving the
    'undersized major' path is live and the bug is specifically the inner-layer band."""
    board = _board(
        nets={1: "12V"},
        tracks=[Track(width=0.5, layer="In1.Cu", net=1, length=20.0)],
        copper_layers=4,
    )
    out = checks.check_trace_currents(board, current_specs={"12V": 2.0})
    assert any(f.id == "trace-12V" and f.severity is Severity.MAJOR for f in out), out


@pytest.mark.xfail(
    reason="bug: check_trace_currents hardcodes external=True (k=0.048), so an inner-layer "
    "(In1.Cu) track is judged with outer-layer ampacity and an undersized inner trace is "
    "not flagged",
    strict=True,
)
def test_inner_layer_undersized_flagged():
    """CORRECT: a 0.5 mm track on In1.Cu carrying 1.07 A is undersized (inner k=0.024 ->
    needs ~0.86 mm) and should be a MAJOR.

    CURRENT: the check assumes external copper (k=0.048 -> only needs ~0.33 mm), so it
    emits nothing. Fails today -> strict xfail.
    """
    board = _board(
        nets={1: "12V"},
        tracks=[Track(width=0.5, layer="In1.Cu", net=1, length=20.0)],
        copper_layers=4,
    )
    out = checks.check_trace_currents(board, current_specs={"12V": 1.07})
    assert any(f.id == "trace-12V" and f.severity is Severity.MAJOR for f in out), out
