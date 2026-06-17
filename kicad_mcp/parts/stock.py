"""Distributor availability: is an MPN a real, in-stock, orderable part?

Two sources, normalized to one shape:

* **JLCPCB / LCSC** -- keyless. Hits the same public endpoint the jlcpcb.com "Parts"
  page uses; returns live stock, price breaks, LCSC part number, Basic/Extended status,
  package and datasheet. NOTE: JLC's keyword search is *fuzzy* -- a nonsense query still
  returns thousands of rows -- so validity REQUIRES an exact MPN/LCSC match, not a
  non-empty result set.
* **DigiKey** -- Product Information API v4, OAuth2 client-credentials (server-side, no
  browser). Activates only when ``DIGIKEY_CLIENT_ID`` + ``DIGIKEY_CLIENT_SECRET`` are set
  (a free key from developer.digikey.com); otherwise it reports ``configured: False`` and
  the JLCPCB half still works.

Pure normalization/matching helpers are separated from the network calls so they test
without a key or a connection.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from kicad_mcp.parts.pull import PartSourceError

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"  # JLC blocks the default urllib UA

_JLC_URL = (
    "https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2"
)
_DK_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"  # noqa: S105 - endpoint, not a secret
_DK_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

_DK_TOKEN: dict = {"val": None, "exp": 0.0}  # cached client-credentials token


# --------------------------------------------------------------------------- #
# JLCPCB / LCSC (keyless)
# --------------------------------------------------------------------------- #
def _jlc_post(keyword: str, page_size: int, timeout: float) -> list[dict]:
    body = json.dumps({"currentPage": 1, "pageSize": page_size, "keyword": keyword}).encode()
    req = urllib.request.Request(  # noqa: S310 - constant https endpoint
        _JLC_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        data = json.loads(r.read().decode("utf-8"))
    return ((data.get("data") or {}).get("componentPageInfo") or {}).get("list") or []


def normalize_jlc(rec: dict) -> dict:
    """One JLC catalog record -> the common availability shape (pure)."""
    breaks = [
        {"qty": p.get("startNumber"), "price": p.get("productPrice")}
        for p in (rec.get("componentPrices") or [])
    ]
    return {
        "lcsc": rec.get("componentCode"),
        "mpn": rec.get("componentModelEn") or "",
        "manufacturer": rec.get("componentBrandEn"),
        "stock": int(rec.get("stockCount") or 0),
        "package": rec.get("componentSpecificationEn"),
        "library_type": "Basic" if rec.get("componentLibraryType") == "base" else "Extended",
        "preferred": bool(rec.get("preferredComponentFlag")),
        "orderable": str(rec.get("isBuyComponent")) == "1" and not rec.get("noBuyReason"),
        "price_breaks": breaks,
        "datasheet": rec.get("dataManualUrl") or rec.get("dataManualOfficialLink"),
        "url": rec.get("lcscGoodsUrl"),
    }


def match_jlc(records: list[dict], query: str) -> dict | None:
    """The record whose MPN or LCSC code equals ``query`` (case-insensitive), or None.

    Guards against JLC's fuzzy search returning unrelated rows for a bad query.
    """
    q = query.strip().upper()
    for rec in records:
        if (rec.get("componentModelEn") or "").upper() == q:
            return rec
        if (rec.get("componentCode") or "").upper() == q:
            return rec
    return None


def search_jlcpcb(query: str, limit: int = 10, timeout: float = 20.0) -> list[dict]:
    """Stock-ranked JLC candidates for a free-text query (the 'find me a part' entry)."""
    return [normalize_jlc(r) for r in _jlc_post(query, limit, timeout)[:limit]]


def check_jlcpcb(query: str, timeout: float = 20.0) -> dict:
    """Validity + live stock for an exact MPN or LCSC code on JLCPCB/LCSC (keyless)."""
    try:
        records = _jlc_post(query, 25, timeout)
    except Exception as e:  # noqa: BLE001 - network -> structured error, never raise
        return {"source": "jlcpcb", "error": f"{type(e).__name__}: {e}"}
    match = match_jlc(records, query)
    if match is None:
        return {
            "source": "jlcpcb",
            "found": False,
            "note": "no exact MPN/LCSC match (JLC keyword search is fuzzy; total>0 is not validity)",
            "candidates": [normalize_jlc(r) for r in records[:5]],
        }
    return {"source": "jlcpcb", "found": True, **normalize_jlc(match)}


# --------------------------------------------------------------------------- #
# DigiKey (Product Information v4, OAuth2 client-credentials)
# --------------------------------------------------------------------------- #
def _digikey_creds() -> tuple[str | None, str | None]:
    return os.environ.get("DIGIKEY_CLIENT_ID"), os.environ.get("DIGIKEY_CLIENT_SECRET")


def have_digikey() -> bool:
    cid, secret = _digikey_creds()
    return bool(cid and secret)


def _digikey_token(timeout: float = 15.0) -> tuple[str, str]:
    """A cached client-credentials access token + the client id. Raises if not configured."""
    cid, secret = _digikey_creds()
    if not (cid and secret):
        raise PartSourceError(
            "DigiKey not configured: set DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET "
            "(free key at developer.digikey.com)"
        )
    now = time.time()
    if _DK_TOKEN["val"] and now < _DK_TOKEN["exp"] - 30:
        return _DK_TOKEN["val"], cid
    body = urllib.parse.urlencode(
        {"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"}
    ).encode()
    req = urllib.request.Request(  # noqa: S310 - constant https endpoint
        _DK_TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        j = json.loads(r.read().decode("utf-8"))
    _DK_TOKEN["val"] = j["access_token"]
    _DK_TOKEN["exp"] = now + int(j.get("expires_in", 600))
    return _DK_TOKEN["val"], cid


def match_digikey(products: list[dict], mpn: str) -> dict | None:
    """The product whose ManufacturerProductNumber equals ``mpn`` (case-insensitive)."""
    q = mpn.strip().upper()
    for p in products:
        if (p.get("ManufacturerProductNumber") or "").upper() == q:
            return p
    return None


def normalize_digikey(p: dict) -> dict:
    """One DigiKey v4 product -> the common availability shape (pure)."""
    var = (p.get("ProductVariations") or [{}])[0]
    breaks = [
        {"qty": b.get("BreakQuantity"), "price": b.get("UnitPrice")}
        for b in (var.get("StandardPricing") or [])
    ]
    return {
        "mpn": p.get("ManufacturerProductNumber"),
        "manufacturer": (p.get("Manufacturer") or {}).get("Name"),
        "stock": int(p.get("QuantityAvailable") or 0),
        "status": (p.get("ProductStatus") or {}).get("Status"),
        "orderable": (p.get("ProductStatus") or {}).get("Status") == "Active"
        and not p.get("Discontinued")
        and not p.get("EndOfLife"),
        "unit_price": p.get("UnitPrice"),
        "price_breaks": breaks,
        "dkpn": var.get("DigiKeyProductNumber"),
        "datasheet": p.get("DatasheetUrl"),
        "url": p.get("ProductUrl"),
    }


def check_digikey(mpn: str, timeout: float = 20.0) -> dict:
    """Validity + live stock for an exact MPN on DigiKey (needs DIGIKEY_CLIENT_ID/SECRET)."""
    if not have_digikey():
        return {
            "source": "digikey",
            "configured": False,
            "note": "set DIGIKEY_CLIENT_ID and DIGIKEY_CLIENT_SECRET (free key: developer.digikey.com)",
        }
    try:
        tok, cid = _digikey_token()
        body = json.dumps({"Keywords": mpn, "Limit": 10}).encode()
        req = urllib.request.Request(  # noqa: S310 - constant https endpoint
            _DK_SEARCH_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {tok}",
                "X-DIGIKEY-Client-Id": cid,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-DIGIKEY-Locale-Site": "US",
                "X-DIGIKEY-Locale-Currency": "USD",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"source": "digikey", "configured": True, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:  # noqa: BLE001 - network -> structured error, never raise
        return {"source": "digikey", "configured": True, "error": f"{type(e).__name__}: {e}"}
    match = match_digikey(data.get("Products") or [], mpn)
    if match is None:
        return {"source": "digikey", "configured": True, "found": False}
    return {"source": "digikey", "configured": True, "found": True, **normalize_digikey(match)}


# --------------------------------------------------------------------------- #
# combined
# --------------------------------------------------------------------------- #
def _available(src: dict) -> bool:
    """A source counts as available iff it found the exact part AND it's in stock."""
    return bool(src.get("found")) and int(src.get("stock") or 0) > 0


def check_stock(mpn: str, timeout: float = 20.0) -> dict:
    """Check an MPN on JLCPCB and DigiKey in parallel; returns both, normalized, plus a
    verdict.

    Each source degrades independently: a network failure or a missing DigiKey key on one
    side never blocks the other. The part is ``valid`` if it is in stock on *either*
    distributor (``available_on`` lists which).
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        fj = ex.submit(check_jlcpcb, mpn, timeout)
        fd = ex.submit(check_digikey, mpn, timeout)
        jlcpcb, digikey = fj.result(), fd.result()
    available_on = [n for n, src in (("jlcpcb", jlcpcb), ("digikey", digikey)) if _available(src)]
    return {
        "mpn": mpn,
        "valid": bool(available_on),  # in stock on at least one distributor
        "available_on": available_on,
        "jlcpcb": jlcpcb,
        "digikey": digikey,
    }
