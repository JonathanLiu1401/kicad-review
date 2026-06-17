"""Distributor availability (JLCPCB keyless + DigiKey) and BOM sourcing sweep.

Pure normalization/matching and the request *logic* (transport mocked) run anywhere. Live
hits are gated behind ``KICAD_REVIEW_NETWORK_TESTS=1``; the DigiKey live test additionally
needs ``DIGIKEY_CLIENT_ID``/``DIGIKEY_CLIENT_SECRET``.
"""

import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kicad_mcp.parts import bom, stock  # noqa: E402

network = pytest.mark.skipif(
    os.environ.get("KICAD_REVIEW_NETWORK_TESTS") != "1",
    reason="set KICAD_REVIEW_NETWORK_TESTS=1 to run live distributor lookups",
)

# realistic single records (fields verified against live responses during development)
_JLC_REC = {
    "componentCode": "C7593",
    "componentModelEn": "NE555DR",
    "componentBrandEn": "Texas Instruments",
    "stockCount": 197903,
    "componentSpecificationEn": "SOIC-8",
    "componentLibraryType": "expand",
    "preferredComponentFlag": True,
    "isBuyComponent": "1",
    "noBuyReason": None,
    "componentPrices": [{"startNumber": 1, "endNumber": 49, "productPrice": 0.1254}],
    "dataManualUrl": "http://example/ds.pdf",
    "lcscGoodsUrl": "http://lcsc/C7593",
}
_DK_PROD = {
    "ManufacturerProductNumber": "NE555DR",
    "Manufacturer": {"Name": "Texas Instruments"},
    "QuantityAvailable": 84231,
    "ProductStatus": {"Id": 0, "Status": "Active"},
    "UnitPrice": 0.50,
    "DatasheetUrl": "http://example/dk.pdf",
    "ProductUrl": "http://digikey/NE555DR",
    "ProductVariations": [
        {
            "DigiKeyProductNumber": "296-1234-1-ND",
            "StandardPricing": [{"BreakQuantity": 1, "UnitPrice": 0.50}],
        }
    ],
}

_SCH = (
    "(kicad_sch\n"
    '\t(symbol\n\t\t(lib_id "Device:R")\n\t\t(at 1 1 0)\n'
    '\t\t(property "Reference" "R1")\n\t\t(property "Value" "10k")\n'
    '\t\t(property "MPN" "RC0603FR-0710KL")\n\t)\n'
    '\t(symbol\n\t\t(lib_id "power:GND")\n'
    '\t\t(property "Reference" "#PWR01")\n\t\t(property "Value" "GND")\n\t)\n'
    '\t(symbol\n\t\t(lib_id "Device:C")\n'
    '\t\t(property "Reference" "C1")\n\t\t(property "Value" "100nF")\n'
    '\t\t(property "lcsc" "C1525")\n\t)\n'  # lowercase field exercises case-insensitive _pick
    '\t(symbol\n\t\t(lib_id "Device:R")\n'
    '\t\t(property "Reference" "R2")\n\t\t(property "Value" "1k")\n\t)\n'  # no MPN -> missing
    ")\n"
)


# --------------------------------------------------------------------------- #
# pure: normalization + matching
# --------------------------------------------------------------------------- #
def test_normalize_jlc():
    n = stock.normalize_jlc(_JLC_REC)
    assert n["lcsc"] == "C7593"
    assert n["mpn"] == "NE555DR"
    assert n["stock"] == 197903
    assert n["library_type"] == "Extended"  # "expand" -> Extended
    assert n["orderable"] is True
    assert n["price_breaks"][0] == {"qty": 1, "price": 0.1254}


def test_normalize_jlc_basic_and_not_orderable():
    rec = {**_JLC_REC, "componentLibraryType": "base", "noBuyReason": "discontinued"}
    n = stock.normalize_jlc(rec)
    assert n["library_type"] == "Basic"
    assert n["orderable"] is False


@pytest.mark.parametrize("q", ["NE555DR", "ne555dr", "C7593", "c7593"])
def test_match_jlc_exact(q):
    assert stock.match_jlc([_JLC_REC], q) is _JLC_REC


def test_match_jlc_rejects_fuzzy_nonmatch():
    # JLC returns unrelated rows for a bad query; an exact match must still be required
    assert stock.match_jlc([_JLC_REC], "SOMETHING_ELSE") is None


def test_normalize_digikey():
    n = stock.normalize_digikey(_DK_PROD)
    assert n["mpn"] == "NE555DR"
    assert n["stock"] == 84231
    assert n["status"] == "Active"
    assert n["orderable"] is True
    assert n["dkpn"] == "296-1234-1-ND"
    assert n["price_breaks"][0] == {"qty": 1, "price": 0.50}


def test_normalize_digikey_obsolete_not_orderable():
    p = {**_DK_PROD, "ProductStatus": {"Status": "Obsolete"}, "EndOfLife": True}
    assert stock.normalize_digikey(p)["orderable"] is False


def test_match_digikey():
    assert stock.match_digikey([_DK_PROD], "ne555dr") is _DK_PROD
    assert stock.match_digikey([_DK_PROD], "nope") is None


# --------------------------------------------------------------------------- #
# request logic with transport mocked (no network)
# --------------------------------------------------------------------------- #
def test_check_jlcpcb_found(monkeypatch):
    monkeypatch.setattr(stock, "_jlc_post", lambda kw, ps, to: [_JLC_REC])
    r = stock.check_jlcpcb("NE555DR")
    assert r["found"] is True
    assert r["lcsc"] == "C7593"


def test_check_jlcpcb_not_found_returns_candidates(monkeypatch):
    monkeypatch.setattr(stock, "_jlc_post", lambda kw, ps, to: [_JLC_REC])
    r = stock.check_jlcpcb("BOGUS_PART")
    assert r["found"] is False
    assert len(r["candidates"]) == 1  # fuzzy rows surfaced as suggestions, not as "valid"


def test_check_jlcpcb_network_error_is_structured(monkeypatch):
    def boom(*a):
        raise RuntimeError("net down")

    monkeypatch.setattr(stock, "_jlc_post", boom)
    assert "error" in stock.check_jlcpcb("X")


def test_search_jlcpcb_logic(monkeypatch):
    monkeypatch.setattr(stock, "_jlc_post", lambda kw, ps, to: [_JLC_REC])
    hits = stock.search_jlcpcb("ne555", limit=5)
    assert len(hits) == 1 and hits[0]["lcsc"] == "C7593"


def test_have_digikey_env(tmp_path, monkeypatch):
    monkeypatch.setattr(stock, "_CREDS_FILE", tmp_path / "none.json")  # ignore any real file
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    assert stock.have_digikey() is False
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "x")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "y")
    assert stock.have_digikey() is True


def test_digikey_creds_from_file(tmp_path, monkeypatch):
    # no env vars, but a credentials file -> configured (the no-restart fallback)
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    f = tmp_path / "creds.json"
    f.write_text(
        '{"DIGIKEY_CLIENT_ID": "fid", "DIGIKEY_CLIENT_SECRET": "fsecret"}', encoding="utf-8"
    )
    monkeypatch.setattr(stock, "_CREDS_FILE", f)
    assert stock.have_digikey() is True
    assert stock._digikey_creds() == ("fid", "fsecret")
    # env vars take precedence over the file
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "eid")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "esecret")
    assert stock._digikey_creds() == ("eid", "esecret")


def test_check_digikey_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(stock, "_CREDS_FILE", tmp_path / "none.json")  # ignore any real file
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    r = stock.check_digikey("NE555DR")
    assert r["configured"] is False
    assert "developer.digikey.com" in r["note"]


def test_check_stock_runs_both_and_verdict(monkeypatch):
    monkeypatch.setattr(
        stock, "check_jlcpcb", lambda m, t=20.0: {"source": "jlcpcb", "found": True, "stock": 100}
    )
    monkeypatch.setattr(
        stock, "check_digikey", lambda m, t=20.0: {"source": "digikey", "configured": False}
    )
    r = stock.check_stock("ANYPART")
    assert r["mpn"] == "ANYPART"
    assert r["valid"] is True  # in stock on JLCPCB alone -> valid
    assert r["available_on"] == ["jlcpcb"]
    assert r["digikey"]["configured"] is False


def test_check_stock_valid_on_digikey_alone(monkeypatch):
    # absent from JLC but in stock on DigiKey -> still valid (either distributor counts)
    monkeypatch.setattr(stock, "check_jlcpcb", lambda m, t=20.0: {"found": False})
    monkeypatch.setattr(stock, "check_digikey", lambda m, t=20.0: {"found": True, "stock": 5})
    r = stock.check_stock("X")
    assert r["valid"] is True
    assert r["available_on"] == ["digikey"]


def test_check_stock_invalid_when_neither_in_stock(monkeypatch):
    # found on JLC but zero stock, absent on DigiKey -> not valid
    monkeypatch.setattr(stock, "check_jlcpcb", lambda m, t=20.0: {"found": True, "stock": 0})
    monkeypatch.setattr(stock, "check_digikey", lambda m, t=20.0: {"found": False})
    r = stock.check_stock("X")
    assert r["valid"] is False
    assert r["available_on"] == []


# --------------------------------------------------------------------------- #
# robustness regressions (work-checker): never raise on malformed API data
# --------------------------------------------------------------------------- #
def test_normalize_jlc_tolerates_dirty_values():
    assert stock.normalize_jlc({"componentModelEn": "X", "stockCount": "1,234"})["stock"] == 1234
    assert stock.normalize_jlc({"stockCount": "N/A"})["stock"] == 0
    assert stock.normalize_jlc({"componentPrices": 5})["price_breaks"] == []  # non-list
    assert stock.normalize_jlc({"componentPrices": ["bad"]})["price_breaks"] == []  # non-dict elems


def test_normalize_digikey_tolerates_dirty_values():
    assert stock.normalize_digikey({"QuantityAvailable": "1,234"})["stock"] == 1234
    assert stock.normalize_digikey({"ProductVariations": [None]})["price_breaks"] == []
    assert stock.normalize_digikey({"ProductVariations": 5})["dkpn"] is None  # non-list


def test_check_jlcpcb_never_raises_on_malformed_record(monkeypatch):
    monkeypatch.setattr(
        stock,
        "_jlc_post",
        lambda kw, ps, to: [
            {"componentModelEn": "X", "componentCode": "C1", "stockCount": "1,234"}
        ],
    )
    r = stock.check_jlcpcb("X")  # must NOT raise even though stockCount isn't a plain int
    assert r["found"] is True and r["stock"] == 1234


# --------------------------------------------------------------------------- #
# BOM extraction + sweep
# --------------------------------------------------------------------------- #
def test_extract_parts(tmp_path):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    parts = {p["ref"]: p for p in bom.extract_parts(sch)}
    assert set(parts) == {"R1", "C1", "R2"}  # #PWR01 (power symbol) excluded
    assert parts["R1"]["mpn"] == "RC0603FR-0710KL"
    assert parts["C1"]["lcsc"] == "C1525"  # picked from the lowercase "lcsc" field
    assert parts["R2"]["mpn"] == "" and parts["R2"]["lcsc"] == ""


def test_check_bom_aggregates_and_flags_missing(tmp_path, monkeypatch):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")
    monkeypatch.setattr(
        bom,
        "check_stock",
        lambda mpn, timeout=20.0: {
            "mpn": mpn,
            "valid": True,
            "available_on": ["jlcpcb"],
            "jlcpcb": {"found": True, "stock": 100},
            "digikey": {"configured": False},
        },
    )
    res = bom.check_bom(sch)
    assert {p["part"] for p in res["parts"]} == {"RC0603FR-0710KL", "C1525"}
    assert all(p["valid"] for p in res["parts"])
    assert [m["ref"] for m in res["missing_mpn"]] == ["R2"]


def test_check_bom_one_bad_part_does_not_abort_sweep(tmp_path, monkeypatch):
    sch = tmp_path / "x.kicad_sch"
    sch.write_text(_SCH, encoding="utf-8")

    def fake(mpn, timeout=20.0):
        if mpn == "C1525":
            raise RuntimeError("boom on this one part")
        return {
            "mpn": mpn,
            "valid": True,
            "available_on": ["jlcpcb"],
            "jlcpcb": {"found": True, "stock": 1},
            "digikey": {},
        }

    monkeypatch.setattr(bom, "check_stock", fake)
    parts = {p["part"]: p for p in bom.check_bom(sch)["parts"]}
    assert set(parts) == {"RC0603FR-0710KL", "C1525"}  # the good part survived
    assert parts["RC0603FR-0710KL"]["valid"] is True
    assert parts["C1525"]["valid"] is False  # the bad one degraded to an error entry
    assert "error" in parts["C1525"]["jlcpcb"]


def test_extract_parts_recurses_into_subsheets(tmp_path):
    (tmp_path / "parent.kicad_sch").write_text(
        '(kicad_sch\n\t(symbol\n\t\t(property "Reference" "R1")\n\t\t(property "MPN" "ROOT")\n\t)\n'
        '\t(sheet\n\t\t(property "Sheetfile" "child.kicad_sch")\n\t)\n)\n',
        encoding="utf-8",
    )
    (tmp_path / "child.kicad_sch").write_text(
        '(kicad_sch\n\t(symbol\n\t\t(property "Reference" "C9")\n\t\t(property "MPN" "SUB")\n\t)\n)\n',
        encoding="utf-8",
    )
    mpns = {p["mpn"] for p in bom.extract_parts(tmp_path / "parent.kicad_sch")}
    assert mpns == {"ROOT", "SUB"}  # sub-sheet component is no longer dropped


def test_extract_parts_missing_root_raises(tmp_path):
    from kicad_mcp.parts.pull import PartSourceError

    with pytest.raises(PartSourceError):
        bom.extract_parts(tmp_path / "does_not_exist.kicad_sch")


# --------------------------------------------------------------------------- #
# live (opt-in)
# --------------------------------------------------------------------------- #
@network
def test_check_jlcpcb_live_real_part():
    r = stock.check_jlcpcb("NE555DR")
    assert r["found"] is True
    assert r["lcsc"] == "C7593"
    assert r["stock"] > 0


@network
def test_check_jlcpcb_live_rejects_garbage():
    # the live endpoint returns thousands of fuzzy rows for this; exact-match gate must hold
    r = stock.check_jlcpcb("definitely_not_a_real_mpn_zzz999")
    assert r["found"] is False


@network
def test_search_jlcpcb_live():
    hits = stock.search_jlcpcb("AMS1117-3.3", limit=5)
    assert hits
    assert all("lcsc" in h and "stock" in h for h in hits)


@network
@pytest.mark.skipif(not stock.have_digikey(), reason="needs DIGIKEY_CLIENT_ID/SECRET")
def test_check_digikey_live():
    r = stock.check_digikey("NE555DR")
    assert r["configured"] is True
    assert "error" not in r
