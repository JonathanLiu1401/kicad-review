import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.review.report import (  # noqa: E402
    Domain,
    Finding,
    Severity,
    sort_findings,
    to_json,
    to_markdown,
)


# --------------------------------------------------------------------------- #
# Severity.rank ordering
# --------------------------------------------------------------------------- #
def test_severity_rank_exact_values():
    # The ranks are the load-bearing sort keys; pin the literal ints.
    assert Severity.BLOCKER.rank == 0
    assert Severity.MAJOR.rank == 1
    assert Severity.MINOR.rank == 2
    assert Severity.NIT.rank == 3
    assert Severity.INFO.rank == 4


def test_severity_rank_strict_ordering():
    assert (
        Severity.BLOCKER.rank
        < Severity.MAJOR.rank
        < Severity.MINOR.rank
        < Severity.NIT.rank
        < Severity.INFO.rank
    )


def test_severity_string_values():
    # Severity is a str-Enum: the .value is the wire form used in JSON/markdown.
    assert Severity.BLOCKER.value == "blocker"
    assert Severity.MAJOR.value == "major"
    assert Severity.MINOR.value == "minor"
    assert Severity.NIT.value == "nit"
    assert Severity.INFO.value == "info"


def test_domain_string_values():
    assert Domain.ELECTRICAL.value == "electrical"
    assert Domain.POWER_THERMAL.value == "power_thermal"
    assert Domain.SIGNAL_INTEGRITY.value == "signal_integrity"
    assert Domain.DFM.value == "dfm"
    assert Domain.BOM.value == "bom"
    assert Domain.HYGIENE.value == "hygiene"


# --------------------------------------------------------------------------- #
# Finding.to_dict
# --------------------------------------------------------------------------- #
def test_to_dict_serializes_enums_to_string_values():
    f = Finding(
        id="x1",
        severity=Severity.BLOCKER,
        domain=Domain.ELECTRICAL,
        title="Unconnected power input",
    )
    d = f.to_dict()
    # Enums must be flattened to their string .value (not Enum members).
    assert d["severity"] == "blocker"
    assert d["domain"] == "electrical"
    assert not isinstance(d["severity"], Severity)
    assert not isinstance(d["domain"], Domain)


def test_to_dict_preserves_all_scalar_and_dict_fields():
    loc = {"sheet": "power.kicad_sch", "refdes": "U3", "net": "+3V3"}
    f = Finding(
        id="x2",
        severity=Severity.MAJOR,
        domain=Domain.POWER_THERMAL,
        title="Regulator undersized",
        detail="Iout exceeds rated current",
        recommendation="Choose a 2A part",
        location=loc,
        evidence="check:reg_current",
        check="reg_current",
    )
    d = f.to_dict()
    assert d["id"] == "x2"
    assert d["title"] == "Regulator undersized"
    assert d["detail"] == "Iout exceeds rated current"
    assert d["recommendation"] == "Choose a 2A part"
    assert d["location"] == loc
    assert d["evidence"] == "check:reg_current"
    assert d["check"] == "reg_current"


def test_to_dict_defaults_are_empty():
    f = Finding(id="x3", severity=Severity.NIT, domain=Domain.HYGIENE, title="t")
    d = f.to_dict()
    assert d["detail"] == ""
    assert d["recommendation"] == ""
    assert d["evidence"] == ""
    assert d["check"] == ""
    assert d["location"] == {}


# --------------------------------------------------------------------------- #
# sort_findings
# --------------------------------------------------------------------------- #
def _shuffled_across_severities_and_domains() -> list[Finding]:
    """Deliberately out-of-order across all 5 severities and several domains.

    Exercises every tier of the sort key:
      * severity.rank   -> BLOCKER first ... INFO last
      * domain.value    -> alphabetical *string* order, NOT enum order
                           (power_thermal sorts AFTER electrical)
      * id              -> lexical tiebreak within same (severity, domain)
    """
    return [
        Finding(id="n1", severity=Severity.NIT, domain=Domain.HYGIENE, title="t-n1"),
        Finding(id="i1", severity=Severity.INFO, domain=Domain.BOM, title="t-i1"),
        Finding(id="b2", severity=Severity.BLOCKER, domain=Domain.POWER_THERMAL, title="t-b2"),
        Finding(id="b1", severity=Severity.BLOCKER, domain=Domain.ELECTRICAL, title="t-b1"),
        Finding(id="mj1", severity=Severity.MAJOR, domain=Domain.DFM, title="t-mj1"),
        Finding(id="mn2", severity=Severity.MINOR, domain=Domain.SIGNAL_INTEGRITY, title="t-mn2"),
        # same (severity, domain) as mn2 -> id tiebreak must put mn1 first
        Finding(id="mn1", severity=Severity.MINOR, domain=Domain.SIGNAL_INTEGRITY, title="t-mn1"),
        # same (severity, domain) as b1 -> id tiebreak must put b0 first
        Finding(id="b0", severity=Severity.BLOCKER, domain=Domain.ELECTRICAL, title="t-b0"),
    ]


def test_sort_findings_exact_order():
    result = sort_findings(_shuffled_across_severities_and_domains())
    assert [f.id for f in result] == ["b0", "b1", "b2", "mj1", "mn1", "mn2", "n1", "i1"]


def test_sort_findings_blocker_first_info_last():
    result = sort_findings(_shuffled_across_severities_and_domains())
    assert result[0].severity is Severity.BLOCKER
    assert result[-1].severity is Severity.INFO


def test_sort_findings_domain_string_order_not_enum_order():
    # Both BLOCKER. ELECTRICAL is declared *before* POWER_THERMAL in the enum,
    # but "electrical" < "power_thermal" alphabetically too, so to make the test
    # meaningful we also compare DFM vs BOM where enum order (DFM before BOM)
    # is the OPPOSITE of string order ("bom" < "dfm").
    findings = [
        Finding(id="a", severity=Severity.BLOCKER, domain=Domain.DFM, title="t"),
        Finding(id="a", severity=Severity.BLOCKER, domain=Domain.BOM, title="t"),
    ]
    result = sort_findings(findings)
    # bom sorts before dfm despite DFM appearing earlier in the Domain enum.
    assert [f.domain for f in result] == [Domain.BOM, Domain.DFM]


def test_sort_findings_does_not_mutate_input():
    original = _shuffled_across_severities_and_domains()
    ids_before = [f.id for f in original]
    sort_findings(original)
    assert [f.id for f in original] == ids_before  # sorted() returns a new list


# --------------------------------------------------------------------------- #
# to_json
# --------------------------------------------------------------------------- #
def test_to_json_parses_and_summary_total_matches_len():
    findings = _shuffled_across_severities_and_domains()
    obj = json.loads(to_json(findings))
    assert obj["summary"]["total"] == len(findings) == 8


def test_to_json_by_severity_and_by_domain_counts():
    findings = _shuffled_across_severities_and_domains()
    obj = json.loads(to_json(findings))
    by_sev = obj["summary"]["by_severity"]
    by_dom = obj["summary"]["by_domain"]
    # 3 blockers (b0,b1,b2), 1 major, 2 minor, 1 nit, 1 info.
    assert by_sev == {"blocker": 3, "major": 1, "minor": 2, "nit": 1, "info": 1}
    # electrical x2 (b0,b1), power_thermal, dfm, signal_integrity x2, hygiene, bom.
    assert by_dom == {
        "electrical": 2,
        "power_thermal": 1,
        "dfm": 1,
        "signal_integrity": 2,
        "hygiene": 1,
        "bom": 1,
    }


def test_to_json_findings_present_and_sorted():
    findings = _shuffled_across_severities_and_domains()
    obj = json.loads(to_json(findings))
    assert len(obj["findings"]) == 8
    # Same canonical order as sort_findings.
    assert [f["id"] for f in obj["findings"]] == [
        "b0",
        "b1",
        "b2",
        "mj1",
        "mn1",
        "mn2",
        "n1",
        "i1",
    ]
    # Enum fields serialized to strings inside the JSON payload.
    assert obj["findings"][0]["severity"] == "blocker"
    assert obj["findings"][0]["domain"] == "electrical"


def test_to_json_meta_passthrough_and_default():
    findings = [Finding(id="a", severity=Severity.INFO, domain=Domain.BOM, title="t")]
    meta = {"project": "Widget", "kicad_version": "8.0"}
    obj = json.loads(to_json(findings, meta))
    assert obj["meta"] == meta
    # meta defaults to an empty dict when omitted.
    obj_default = json.loads(to_json(findings))
    assert obj_default["meta"] == {}


def test_to_json_unicode_title_roundtrips():
    title = "Trace width 0.2µm below 1µm minimum — µ-strip ⊕"
    findings = [
        Finding(id="u1", severity=Severity.MAJOR, domain=Domain.DFM, title=title),
    ]
    raw = to_json(findings)
    obj = json.loads(raw)
    # The non-ASCII characters survive the json.dumps -> json.loads roundtrip.
    assert obj["findings"][0]["title"] == title
    assert "µ" in obj["findings"][0]["title"]


def test_to_json_empty_findings():
    obj = json.loads(to_json([]))
    assert obj["summary"]["total"] == 0
    assert obj["summary"]["by_severity"] == {}
    assert obj["summary"]["by_domain"] == {}
    assert obj["findings"] == []


# --------------------------------------------------------------------------- #
# to_markdown
# --------------------------------------------------------------------------- #
def _one_per_severity_findings() -> list[Finding]:
    return [
        Finding(id="b", severity=Severity.BLOCKER, domain=Domain.ELECTRICAL, title="Block title"),
        Finding(id="mj", severity=Severity.MAJOR, domain=Domain.POWER_THERMAL, title="Major title"),
        Finding(id="mn", severity=Severity.MINOR, domain=Domain.DFM, title="Minor title"),
        Finding(id="n", severity=Severity.NIT, domain=Domain.HYGIENE, title="Nit title"),
        Finding(id="i", severity=Severity.INFO, domain=Domain.BOM, title="Info title"),
    ]


def test_to_markdown_header_and_meta():
    findings = _one_per_severity_findings()
    md = to_markdown(findings, {"project": "Widget Board", "kicad_version": "8.0.5"})
    assert "Design review" in md
    assert "Widget Board" in md  # project name from meta
    assert "8.0.5" in md  # kicad_version from meta


def test_to_markdown_renders_every_finding_title():
    findings = _one_per_severity_findings()
    md = to_markdown(findings, {"project": "P"})
    for f in findings:
        assert f.title in md, f"title not rendered: {f.title!r}"


def test_to_markdown_per_domain_section_headers():
    findings = _one_per_severity_findings()
    md = to_markdown(findings, {"project": "P"})
    # Section headers come from domain.value.replace('_',' ').title().
    assert "## Electrical" in md
    assert "## Power Thermal" in md  # underscore -> space + title-case
    # Quirky title-casing of short acronyms (these are NOT upper-cased).
    assert "## Dfm" in md
    assert "## Bom" in md
    assert "## Hygiene" in md


def test_to_markdown_renders_location_detail_recommendation_evidence():
    f = Finding(
        id="rich",
        severity=Severity.MAJOR,
        domain=Domain.ELECTRICAL,
        title="Rich finding",
        detail="Some detail body.",
        recommendation="Do the thing.",
        location={"refdes": "R7", "net": "SDA"},
        evidence="check:rich_one",
    )
    md = to_markdown([f], {"project": "P"})
    assert "Rich finding" in md
    assert "Some detail body." in md
    assert "Do the thing." in md
    assert "refdes=R7" in md
    assert "net=SDA" in md
    assert "evidence: check:rich_one" in md


def test_to_markdown_single_info_finding_renders():
    findings = [
        Finding(id="solo", severity=Severity.INFO, domain=Domain.BOM, title="Lone info"),
    ]
    md = to_markdown(findings, {"project": "P"})
    assert "Lone info" in md
    assert "## Bom" in md
    assert "(total 1)" in md
    # A non-empty list must NOT hit the "no findings" path.
    assert "No deterministic findings" not in md


def test_to_markdown_empty_list_hits_no_findings_path():
    md = to_markdown([], {"project": "Empty Board"})
    assert "No deterministic findings" in md
    assert "Design review" in md
    assert "Empty Board" in md
    assert "(total 0)" in md


def test_to_markdown_default_project_when_meta_missing():
    md = to_markdown([])
    # Falls back to the default project label.
    assert "KiCad design" in md


def test_to_markdown_omits_kicad_version_line_when_absent():
    md = to_markdown([], {"project": "P"})
    # The "_KiCad ... generated by_" line is gated on meta['kicad_version'].
    assert "generated by kicad-review" not in md


def test_to_markdown_duplicate_ids_both_kept():
    findings = [
        Finding(id="dup", severity=Severity.MAJOR, domain=Domain.DFM, title="First dup"),
        Finding(id="dup", severity=Severity.MAJOR, domain=Domain.DFM, title="Second dup"),
    ]
    md = to_markdown(findings, {"project": "P"})
    # Both findings sharing an id are still rendered independently.
    assert "First dup" in md
    assert "Second dup" in md
    # And both are counted.
    assert "(total 2)" in md


def test_to_json_duplicate_ids_both_kept():
    findings = [
        Finding(id="dup", severity=Severity.MAJOR, domain=Domain.DFM, title="First dup"),
        Finding(id="dup", severity=Severity.MAJOR, domain=Domain.DFM, title="Second dup"),
    ]
    obj = json.loads(to_json(findings))
    assert obj["summary"]["total"] == 2
    titles = sorted(f["title"] for f in obj["findings"])
    assert titles == ["First dup", "Second dup"]


# --------------------------------------------------------------------------- #
# Markdown summary-header helpers (used by the confirmed-bug test below)
# --------------------------------------------------------------------------- #
def _findings_header_line(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("**Findings:**"):
            return line
    raise AssertionError("no **Findings:** header line found in markdown")


def _parse_header_total(header: str) -> int:
    m = re.search(r"\(total (\d+)\)", header)
    assert m is not None, f"no '(total N)' in header: {header!r}"
    return int(m.group(1))


def _parse_header_displayed_sum(header: str) -> int:
    # Sum only the per-severity counts shown BEFORE the "(total N)" suffix,
    # so the literal total digits are never double-counted.
    prefix = header.split("(total", 1)[0]
    # Each rendered segment is "<icon> <count> <severity-word>".
    counts = re.findall(r"(\d+)\s+(?:blocker|major|minor|nit|info)\b", prefix)
    return sum(int(c) for c in counts)


def test_header_helpers_agree_when_no_info():
    # Sanity check the parsers on a case the producer renders fully:
    # with only blocker/major/minor/nit present, displayed sum == total.
    findings = [
        Finding(id="b", severity=Severity.BLOCKER, domain=Domain.ELECTRICAL, title="t"),
        Finding(id="mj", severity=Severity.MAJOR, domain=Domain.DFM, title="t"),
        Finding(id="mn", severity=Severity.MINOR, domain=Domain.DFM, title="t"),
        Finding(id="n", severity=Severity.NIT, domain=Domain.HYGIENE, title="t"),
    ]
    header = _findings_header_line(to_markdown(findings, {"project": "P"}))
    assert _parse_header_total(header) == 4
    assert _parse_header_displayed_sum(header) == 4


# --------------------------------------------------------------------------- #
# CONFIRMED BUG: to_markdown summary header omits INFO from the per-severity
# breakdown, yet "(total N)" counts it -> displayed counts do not sum to total.
# This test asserts the CORRECT/consistent behavior and is expected to FAIL
# against the current implementation, hence xfail(strict=True).
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    reason="to_markdown summary header omits INFO while total counts it",
    strict=True,
)
def test_markdown_header_counts_sum_to_total_includes_info():
    findings = [
        Finding(id="b", severity=Severity.BLOCKER, domain=Domain.ELECTRICAL, title="t-block"),
        Finding(id="i1", severity=Severity.INFO, domain=Domain.HYGIENE, title="t-info1"),
        Finding(id="i2", severity=Severity.INFO, domain=Domain.HYGIENE, title="t-info2"),
    ]
    header = _findings_header_line(to_markdown(findings, {"project": "P"}))
    total = _parse_header_total(header)
    displayed = _parse_header_displayed_sum(header)
    assert total == 3  # blocker(1) + info(2)
    # CORRECT behavior: the per-severity counts shown in the header should add
    # up to the stated total. Current code leaves INFO out of the loop, so the
    # displayed sum is 1 while total is 3 -> this assertion fails -> xfail.
    assert displayed == total
