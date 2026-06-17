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


def test_have_digikey_env(monkeypatch):
    monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
    monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
    assert stock.have_digikey() is False
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "x")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "y")
    assert stock.have_digikey() is True


def test_check_digikey_unconfigured(monkeypatch):
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
