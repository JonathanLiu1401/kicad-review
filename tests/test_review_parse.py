"""Comprehensive unit tests for ``kicad_mcp.review.parse``.

Every test builds tiny synthetic S-expression / JSON snippets, writes them to a
``tmp_path`` file, and parses them back -- exercising the real I/O path (the
parsers ``read_text`` their arguments) without needing KiCad. A final tier
re-parses the genuine PERIPH board when it is present on disk.

Design rules honored here:
  * Non-vacuous: every absence assertion is paired with a matching presence
    assertion, so a parser that silently dropped *everything* would still fail.
  * The module is believed correct; nothing is expected to xfail.
"""

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.review import parse  # noqa: E402

# Real board: parse it too when it exists, to prove the synthetic fixtures match
# reality. Overridable so the suite is portable to other machines / CI.
REAL_BOARD = Path(
    os.environ.get(
        "KICAD_REVIEW_TEST_BOARD",
        r"C:/Users/jonny/Desktop/Trellis/In-Pipe-Hardware/v0/PERIPH/PERIPH.kicad_pcb",
    )
)
real_board_only = pytest.mark.skipif(not REAL_BOARD.exists(), reason="no real board")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# low-level nav helpers
# --------------------------------------------------------------------------- #
def test_sym_unwraps_symbol_and_passes_scalars():
    assert parse._sym(parse.sexpdata.Symbol("foo")) == "foo"
    assert parse._sym(42) == 42
    assert parse._sym("bar") == "bar"


def test_head_get_getval_getall():
    node = parse.sexpdata.loads('(root (a 1) (b "two") (a 3))')
    assert parse._head(node) == "root"
    assert parse._head([]) is None
    assert parse._head("scalar") is None
    # _get returns the first child with the matching head
    assert parse._sym(parse._get(node, "a")[1]) == 1
    assert parse._get(node, "missing") is None
    # _getval reads the second element, with a default fallback
    assert parse._getval(node, "b") == "two"
    assert parse._getval(node, "missing", "fallback") == "fallback"
    # _getall returns every matching child
    assert len(parse._getall(node, "a")) == 2
    assert parse._getall("not-a-list", "a") == []


def test_get_skips_non_list_children():
    # _get must walk past bare scalar children (which have no head) and still
    # find the real keyed child that follows them.
    node = parse.sexpdata.loads('(root bareword 7 (target "hit"))')
    assert parse._getval(node, "target") == "hit"  # presence: found past scalars
    assert parse._get(node, "bareword") is None  # absence: scalars aren't matched


def test_get_on_non_list_returns_none():
    # _get / _getval / _head are all defensive against being handed a scalar.
    assert parse._get("scalar", "anything") is None
    assert parse._get(123, "anything") is None
    assert parse._getval(None, "k", "dflt") == "dflt"  # presence of the default


# --------------------------------------------------------------------------- #
# parse_board: tracks (segments + arcs)
# --------------------------------------------------------------------------- #
def test_segment_and_arc_both_become_tracks_with_geometry(tmp_path):
    pcb = """
    (kicad_pcb
      (net 1 "SIG")
      (net 2 "PWR")
      (segment (start 0 0) (end 3 0) (width 0.25) (layer "F.Cu") (net 1))
      (arc (start 0 0) (mid 1.5 9) (end 3 4) (width 0.4) (layer "B.Cu") (net 2))
    )
    """
    board = parse.parse_board(_write(tmp_path, "seg.kicad_pcb", pcb))

    assert len(board.tracks) == 2
    seg, arc = board.tracks

    # straight segment: width/layer/net captured, length = euclidean distance
    assert seg.width == 0.25
    assert seg.layer == "F.Cu"
    assert seg.net == 1
    assert seg.length == pytest.approx(3.0)

    # arc: length is the chord between start and end (the bulging mid is ignored)
    assert arc.width == 0.4
    assert arc.layer == "B.Cu"
    assert arc.net == 2
    assert arc.length == pytest.approx(5.0)  # 3-4-5, NOT routed through mid


def test_track_with_missing_endpoints_has_zero_length(tmp_path):
    # exercises the `length ... else 0.0` branch: a segment with no start/end.
    pcb = """
    (kicad_pcb
      (net 1 "SIG")
      (segment (width 0.2) (layer "F.Cu") (net 1))
    )
    """
    board = parse.parse_board(_write(tmp_path, "nolen.kicad_pcb", pcb))
    assert len(board.tracks) == 1  # presence: the track still exists
    assert board.tracks[0].width == 0.2  # and carries its other fields
    assert board.tracks[0].length == 0.0  # but length collapses to 0


# --------------------------------------------------------------------------- #
# parse_board: vias
# --------------------------------------------------------------------------- #
def test_vias_parsed_with_size_drill_net(tmp_path):
    pcb = """
    (kicad_pcb
      (net 4 "GND")
      (via (at 10 10) (size 0.8) (drill 0.4) (net 4))
    )
    """
    board = parse.parse_board(_write(tmp_path, "via.kicad_pcb", pcb))
    assert len(board.vias) == 1
    via = board.vias[0]
    assert via.size == pytest.approx(0.8)
    assert via.drill == pytest.approx(0.4)
    assert via.net == 4


# --------------------------------------------------------------------------- #
# parse_board: footprints
# --------------------------------------------------------------------------- #
def test_footprint_reference_value_and_at_xy(tmp_path):
    pcb = """
    (kicad_pcb
      (footprint "Lib:R_0402" (layer "F.Cu")
        (at 12.5 34.0)
        (property "Reference" "R1")
        (property "Value" "10k")
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "fp.kicad_pcb", pcb))
    assert len(board.footprints) == 1
    fp = board.footprints[0]
    assert fp.ref == "R1"
    assert fp.value == "10k"
    assert fp.layer == "F.Cu"
    assert fp.x == pytest.approx(12.5)
    assert fp.y == pytest.approx(34.0)


def test_footprint_at_with_angle_keeps_xy_drops_angle(tmp_path):
    # (at x y angle): the rotation must be ignored, x/y still captured.
    pcb = """
    (kicad_pcb
      (footprint "Lib:U" (layer "B.Cu")
        (at 1.0 2.0 180)
        (property "Reference" "U7")
        (property "Value" "MCU")
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "fpang.kicad_pcb", pcb))
    fp = board.footprints[0]
    assert fp.x == pytest.approx(1.0)
    assert fp.y == pytest.approx(2.0)
    assert fp.layer == "B.Cu"  # presence: still parsed despite the angle field
    assert fp.ref == "U7"


def test_footprint_missing_at_defaults_to_origin(tmp_path):
    pcb = """
    (kicad_pcb
      (footprint "Lib:TP" (layer "F.Cu")
        (property "Reference" "TP1")
        (property "Value" "TEST")
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "fpnoat.kicad_pcb", pcb))
    fp = board.footprints[0]
    assert fp.ref == "TP1"  # presence: the footprint is real
    assert fp.x == 0.0  # absence of (at ...) -> origin
    assert fp.y == 0.0


def test_footprint_duplicate_reference_and_value_last_wins(tmp_path):
    pcb = """
    (kicad_pcb
      (footprint "Lib:C" (layer "F.Cu")
        (at 0 0)
        (property "Reference" "C1")
        (property "Reference" "C2")
        (property "Value" "1uF")
        (property "Value" "2uF")
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "fpdup.kicad_pcb", pcb))
    fp = board.footprints[0]
    assert fp.ref == "C2"  # later property overwrites the earlier one
    assert fp.value == "2uF"


# --------------------------------------------------------------------------- #
# parse_board: nets dict (top-level only) -- the critical pad-net exclusion
# --------------------------------------------------------------------------- #
def test_top_level_nets_collected_pad_nets_excluded(tmp_path):
    # CRITICAL: a pad-level (net N "name") nested in a footprint must NOT land in
    # board.nets -- only top-level (net id name) entries do. We still prove the
    # pad net was parsed (it shows up in the footprint's net set), so the
    # exclusion assertion below cannot pass vacuously.
    pcb = """
    (kicad_pcb
      (net 1 "3.3V")
      (net 2 "GND")
      (footprint "Lib:R" (layer "F.Cu")
        (at 0 0)
        (property "Reference" "R1")
        (property "Value" "10k")
        (pad "1" smd roundrect (net 1 "3.3V"))
        (pad "2" smd roundrect (net 7 "INTERNAL-PADNET"))
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "padnet.kicad_pcb", pcb))

    # presence: the two real top-level nets are there
    assert board.nets == {1: "3.3V", 2: "GND"}
    # absence: the pad-only net id 7 never entered the board net table
    assert 7 not in board.nets
    # ...but it WAS parsed -- the footprint records the net ids its pads touch
    assert board.footprints[0].nets == {1, 7}


def test_net_name_helper_known_and_unknown(tmp_path):
    pcb = """
    (kicad_pcb
      (net 5 "VBUS")
    )
    """
    board = parse.parse_board(_write(tmp_path, "netname.kicad_pcb", pcb))
    assert board.net_name(5) == "VBUS"  # known id -> its name
    assert board.net_name(99) == "<net99>"  # unknown id -> synthetic placeholder


def test_malformed_net_node_too_short_is_skipped(tmp_path):
    # A (net 1) with no name (len < 3) is skipped; a well-formed sibling is kept.
    pcb = """
    (kicad_pcb
      (net 1)
      (net 2 "GOOD")
    )
    """
    board = parse.parse_board(_write(tmp_path, "shortnet.kicad_pcb", pcb))
    assert 2 in board.nets  # presence: the valid net survives
    assert board.nets[2] == "GOOD"
    assert 1 not in board.nets  # absence: the malformed net is dropped


# --------------------------------------------------------------------------- #
# parse_board: zones -> poured_nets
# --------------------------------------------------------------------------- #
def test_zones_named_poured_nets_empty_name_skipped(tmp_path):
    pcb = """
    (kicad_pcb
      (net 1 "GND")
      (zone (net 1) (net_name "GND") (layer "F.Cu"))
      (zone (net 0) (net_name "") (layer "B.Cu"))
    )
    """
    board = parse.parse_board(_write(tmp_path, "zone.kicad_pcb", pcb))
    assert "GND" in board.poured_nets  # presence: named pour recorded
    assert "" not in board.poured_nets  # absence: empty-name pour skipped
    assert board.poured_nets == {"GND"}


# --------------------------------------------------------------------------- #
# parse_board: stackup (_parse_stackup, reached via parse_board)
# --------------------------------------------------------------------------- #
def test_stackup_four_copper_layers_and_one_oz(tmp_path):
    pcb = """
    (kicad_pcb
      (layers
        (0 "F.Cu" signal)
        (1 "In1.Cu" signal)
        (2 "In2.Cu" signal)
        (3 "B.Cu" signal)
        (5 "F.SilkS" user)
        (25 "Edge.Cuts" user)
      )
      (setup
        (stackup
          (layer "F.SilkS" (type "Top Silk Screen"))
          (layer "F.Cu" (type "copper") (thickness 0.035))
          (layer "B.Cu" (type "copper") (thickness 0.07))
        )
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "stack4.kicad_pcb", pcb))
    # four names end in ".Cu" -> 4 copper layers (not the silk / edge entries)
    assert board.copper_layers == 4
    # first copper stackup layer is 0.035 mm = 1.0 oz (parser breaks on the first)
    assert board.copper_oz == pytest.approx(1.0)


def test_stackup_thickness_computes_non_default_oz(tmp_path):
    # Guards against the 1.0 default being vacuously correct: 0.07 mm -> 2.0 oz.
    pcb = """
    (kicad_pcb
      (layers
        (0 "F.Cu" signal)
        (3 "B.Cu" signal)
      )
      (setup
        (stackup
          (layer "F.Cu" (type "copper") (thickness 0.07))
        )
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "stack2oz.kicad_pcb", pcb))
    assert board.copper_layers == 2  # two .Cu names
    assert board.copper_oz == pytest.approx(2.0)  # 0.07 / 0.035


def test_stackup_defaults_when_absent(tmp_path):
    # No (layers) and no (setup) at all -> documented defaults 2 / 1.0 oz.
    pcb = '(kicad_pcb (net 1 "X"))'
    board = parse.parse_board(_write(tmp_path, "stackdef.kicad_pcb", pcb))
    assert board.copper_layers == 2
    assert board.copper_oz == pytest.approx(1.0)
    # presence anchor: the board still parsed its net, so defaults aren't from a
    # totally empty/garbage parse.
    assert board.nets == {1: "X"}


def test_stackup_layers_present_without_cu_keeps_default_count(tmp_path):
    # (layers) block exists but has NO ".Cu" names -> copper_layers stays at 2.
    # Exercises the `if cu:` false branch while the block is still non-empty.
    pcb = """
    (kicad_pcb
      (layers
        (5 "F.SilkS" user)
        (25 "Edge.Cuts" user)
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "nocu.kicad_pcb", pcb))
    assert board.copper_layers == 2  # no .Cu names -> default
    assert board.copper_oz == pytest.approx(1.0)


def test_stackup_setup_without_stackup_keeps_default_oz(tmp_path):
    # (setup) present but no (stackup) child -> oz default, count from (layers).
    pcb = """
    (kicad_pcb
      (layers
        (0 "F.Cu" signal)
        (3 "B.Cu" signal)
      )
      (setup
        (pad_to_mask_clearance 0.05)
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "nostack.kicad_pcb", pcb))
    assert board.copper_layers == 2  # presence: counted from (layers)
    assert board.copper_oz == pytest.approx(1.0)  # no stackup -> default oz


def test_stackup_cu_layer_without_thickness_keeps_default_oz(tmp_path):
    # A copper stackup layer with no (thickness ...) -> `if th:` false, the loop
    # moves on; the LATER copper layer (with thickness) supplies the weight.
    pcb = """
    (kicad_pcb
      (layers
        (0 "F.Cu" signal)
        (3 "B.Cu" signal)
      )
      (setup
        (stackup
          (layer "F.Cu" (type "copper"))
          (layer "B.Cu" (type "copper") (thickness 0.07))
        )
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "nothick.kicad_pcb", pcb))
    assert board.copper_layers == 2  # presence
    # first .Cu layer has no thickness (skipped); second yields 0.07 -> 2.0 oz
    assert board.copper_oz == pytest.approx(2.0)


def test_stackup_only_non_cu_layers_keeps_default_oz(tmp_path):
    # (stackup) present and iterated, but it contains NO copper layers at all, so
    # the loop runs to completion without ever hitting the break -> oz default.
    pcb = """
    (kicad_pcb
      (layers
        (0 "F.Cu" signal)
        (3 "B.Cu" signal)
      )
      (setup
        (stackup
          (layer "F.SilkS" (type "Top Silk Screen"))
          (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
        )
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "stacknocu.kicad_pcb", pcb))
    assert board.copper_layers == 2  # presence: counted from (layers)
    # the silk/mask thicknesses are NOT copper -> oz stays at the 1.0 default
    assert board.copper_oz == pytest.approx(1.0)


def test_stackup_bad_thickness_keeps_default_oz(tmp_path):
    # A non-numeric thickness is suppressed (TypeError/ValueError) -> oz stays 1.0,
    # while layer counting still works from the (layers) block.
    pcb = """
    (kicad_pcb
      (layers
        (0 "F.Cu" signal)
        (3 "B.Cu" signal)
      )
      (setup
        (stackup
          (layer "F.Cu" (type "copper") (thickness "oops"))
        )
      )
    )
    """
    board = parse.parse_board(_write(tmp_path, "stackbad.kicad_pcb", pcb))
    assert board.copper_layers == 2  # presence: counting unaffected
    assert board.copper_oz == pytest.approx(1.0)  # bad thickness -> default oz


# --------------------------------------------------------------------------- #
# parse_board: degenerate inputs
# --------------------------------------------------------------------------- #
def test_empty_board_sane_defaults(tmp_path):
    board = parse.parse_board(_write(tmp_path, "empty.kicad_pcb", "(kicad_pcb)"))
    # genuinely empty board -> empty collections + documented stackup defaults
    assert board.nets == {}
    assert board.tracks == []
    assert board.vias == []
    assert board.footprints == []
    assert board.poured_nets == set()
    assert board.copper_layers == 2  # presence: defaults still applied
    assert board.copper_oz == pytest.approx(1.0)


def test_malformed_nodes_produce_default_valued_objects(tmp_path):
    # A bare (segment)/(via)/(footprint) with no children is NOT dropped -- it
    # yields a default-valued object. (Only malformed nets / empty zones vanish.)
    pcb = """
    (kicad_pcb
      (segment)
      (via)
      (footprint)
      (zone)
    )
    """
    board = parse.parse_board(_write(tmp_path, "malformed.kicad_pcb", pcb))

    assert len(board.tracks) == 1  # presence, then default field values:
    assert board.tracks[0].net == -1
    assert board.tracks[0].width == 0.0
    assert board.tracks[0].layer == ""
    assert board.tracks[0].length == 0.0

    assert len(board.vias) == 1
    assert board.vias[0].net == -1
    assert board.vias[0].size == 0.0

    assert len(board.footprints) == 1
    assert board.footprints[0].ref == ""
    assert board.footprints[0].value == ""
    assert board.footprints[0].nets == set()

    # the empty (zone) carries no net_name -> nothing poured (true absence here)
    assert board.poured_nets == set()


# --------------------------------------------------------------------------- #
# parse_pro
# --------------------------------------------------------------------------- #
def test_parse_pro_classes_assignments_rules(tmp_path):
    pro = """
    {
      "net_settings": {
        "classes": [
          {"name": "Default", "track_width": 0.2},
          {"name": "Power", "track_width": 0.5}
        ],
        "netclass_assignments": [
          {"pattern": "GND", "netclass": "Power"},
          {"net": "VBUS", "netclass": "Power"}
        ]
      },
      "board": {"design_settings": {"rules": {"min_track_width": 0.15}}},
      "erc": {"rule_severities": {}}
    }
    """
    ps = parse.parse_pro(_write(tmp_path, "p.kicad_pro", pro))
    assert len(ps.net_classes) == 2
    assert ps.net_classes[1]["name"] == "Power"
    # assignment falls back from "pattern" to "net" when pattern is absent
    assert ps.net_class_assignments["GND"] == "Power"
    assert ps.net_class_assignments["VBUS"] == "Power"
    assert ps.design_rules["min_track_width"] == pytest.approx(0.15)


def test_parse_pro_skips_non_dict_assignments(tmp_path):
    # netclass_assignments may contain stray non-dict junk; those are skipped,
    # while a well-formed dict assignment alongside them is still collected.
    pro = """
    {
      "net_settings": {
        "classes": [],
        "netclass_assignments": [
          "not-a-dict",
          {"pattern": "GND", "netclass": "Power"}
        ]
      }
    }
    """
    ps = parse.parse_pro(_write(tmp_path, "pjunk.kicad_pro", pro))
    assert ps.net_class_assignments == {"GND": "Power"}  # dict kept, string ignored


def test_parse_pro_erc_keeps_only_ignore_and_warning(tmp_path):
    pro = """
    {
      "net_settings": {"classes": []},
      "erc": {"rule_severities": {
        "silk_overlap": "warning",
        "courtyards_overlap": "ignore",
        "unconnected_items": "error"
      }}
    }
    """
    ps = parse.parse_pro(_write(tmp_path, "perc.kicad_pro", pro))
    # presence: the overridden (ignore/warning) severities are kept
    assert ps.erc_severities["silk_overlap"] == "warning"
    assert ps.erc_severities["courtyards_overlap"] == "ignore"
    # absence: the default "error" severity is dropped
    assert "unconnected_items" not in ps.erc_severities
    assert set(ps.erc_severities) == {"silk_overlap", "courtyards_overlap"}


def test_parse_pro_missing_keys_give_empty_structures(tmp_path):
    ps = parse.parse_pro(_write(tmp_path, "pempty.kicad_pro", "{}"))
    assert ps.net_classes == []
    assert ps.net_class_assignments == {}
    assert ps.design_rules == {}
    assert ps.erc_severities == {}


def test_parse_pro_non_default_present_alongside_empties(tmp_path):
    # Pair the empty-structure behavior with a populated one so "empty" is a real
    # outcome of parsing, not a parser that ignores everything.
    pro = '{"net_settings": {"classes": [{"name": "Default"}]}}'
    ps = parse.parse_pro(_write(tmp_path, "pmix.kicad_pro", pro))
    assert ps.net_classes == [{"name": "Default"}]  # presence
    assert ps.erc_severities == {}  # absence, given the same parse


# --------------------------------------------------------------------------- #
# parse_netlist
# --------------------------------------------------------------------------- #
def test_parse_netlist_components_nets_libparts(tmp_path):
    net = """
    (export (version "E")
      (components
        (comp (ref "R1") (value "10k") (footprint "R_0402"))
        (comp (ref "U1") (value "MCU") (footprint "QFN32"))
      )
      (libparts
        (libpart (lib "Device") (part "R")
          (pins
            (pin (num "1") (name "~") (type "passive"))
            (pin (num "2") (name "~") (type "passive"))
          )
        )
      )
      (nets
        (net (code "1") (name "GND")
          (node (ref "R1") (pin "2") (pintype "passive"))
          (node (ref "U1") (pin "10") (pintype "power_in"))
        )
      )
    )
    """
    nl = parse.parse_netlist(_write(tmp_path, "n.net", net))

    # components: ref / value / footprint
    assert len(nl.components) == 2
    assert nl.components[0] == {"ref": "R1", "value": "10k", "footprint": "R_0402"}
    assert nl.components[1]["ref"] == "U1"

    # nets: nodes carry pintype mapped to the "type" key (NOT "pintype")
    assert len(nl.nets) == 1
    gnd = nl.nets[0]
    assert gnd["name"] == "GND"
    assert gnd["code"] == "1"
    assert len(gnd["nodes"]) == 2
    assert gnd["nodes"][0] == {"ref": "R1", "pin": "2", "type": "passive"}
    assert gnd["nodes"][1]["type"] == "power_in"

    # libpart pin_types: keyed "lib:part" -> {pin_num: type}
    assert nl.pin_types["Device:R"] == {"1": "passive", "2": "passive"}


def test_parse_netlist_libpart_without_pins_maps_to_empty(tmp_path):
    # A libpart that declares no (pins ...) still registers under its key with an
    # empty pin map -- proving the key is present even when there is nothing to
    # enumerate (the `if pins:` false branch).
    net = """
    (export
      (libparts
        (libpart (lib "Mechanical") (part "MountingHole"))
      )
    )
    """
    nl = parse.parse_netlist(_write(tmp_path, "nopins.net", net))
    assert "Mechanical:MountingHole" in nl.pin_types  # presence: key exists
    assert nl.pin_types["Mechanical:MountingHole"] == {}  # absence: no pins


def test_parse_netlist_empty_sections(tmp_path):
    # No components/libparts/nets nodes at all -> empty lists/dict, but the file
    # is a valid export, so this is a real (non-vacuous) parse.
    net = '(export (version "E"))'
    nl = parse.parse_netlist(_write(tmp_path, "nempty.net", net))
    assert nl.components == []
    assert nl.nets == []
    assert nl.pin_types == {}


def test_parse_netlist_node_pintype_distinct_from_libpart_type(tmp_path):
    # Guards the easy cross-wire: a NODE uses (pintype ...), a LIBPART pin uses
    # (type ...). Each must surface under "type", read from its own sub-key.
    net = """
    (export
      (libparts
        (libpart (lib "L") (part "P")
          (pins (pin (num "A") (type "input")))
        )
      )
      (nets
        (net (code "9") (name "SIG")
          (node (ref "U1") (pin "A") (pintype "output"))
        )
      )
    )
    """
    nl = parse.parse_netlist(_write(tmp_path, "ndistinct.net", net))
    assert nl.pin_types["L:P"]["A"] == "input"  # from libpart (type ...)
    assert nl.nets[0]["nodes"][0]["type"] == "output"  # from node (pintype ...)


# --------------------------------------------------------------------------- #
# realism: re-parse the genuine PERIPH board when present
# --------------------------------------------------------------------------- #
@real_board_only
def test_real_board_overall_shape():
    board = parse.parse_board(REAL_BOARD)
    assert len(board.tracks) > 500  # PERIPH has hundreds of routed segments
    assert len(board.vias) > 0
    assert board.copper_layers == 4  # F.Cu / In1.Cu / In2.Cu / B.Cu


@real_board_only
def test_real_board_has_ground_pour():
    board = parse.parse_board(REAL_BOARD)
    assert "GND" in board.poured_nets


@real_board_only
def test_real_board_nets_and_footprints_populated():
    board = parse.parse_board(REAL_BOARD)
    assert len(board.nets) > 0
    assert len(board.footprints) > 0
    # net_name round-trips on a real id, proving nets dict is keyed by int id
    some_id = next(iter(board.nets))
    assert board.net_name(some_id) == board.nets[some_id]
