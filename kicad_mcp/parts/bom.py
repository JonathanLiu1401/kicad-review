"""Sweep a whole schematic's sourcing: pull every component's MPN/LCSC and check stock.

``extract_parts`` reads each placed symbol's properties (MPN / LCSC by their common field
names); ``check_bom`` de-duplicates by part number and checks each on JLCPCB + DigiKey in
parallel, so one command answers "is my entire BOM orderable and in stock?".
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sexpdata

from kicad_mcp.parts.stock import check_stock
from kicad_mcp.review.parse import _getall, _head, _sym

# field names a KiCad symbol might carry an MPN / LCSC code under (matched case-insensitively)
_MPN_FIELDS = (
    "MPN",
    "Manufacturer Part Number",
    "Mfr Part #",
    "MfrPN",
    "Manufacturer_Part_Number",
    "Mfr. No",
    "Part Number",
    "VPN",
)
_LCSC_FIELDS = ("LCSC", "LCSC Part #", "LCSC Part Number", "LCSC#", "JLCPCB Part #")


def _properties(node) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in _getall(node, "property"):
        if isinstance(p, list) and len(p) >= 3:
            out[str(_sym(p[1]))] = str(_sym(p[2]))
    return out


def _pick(props: dict[str, str], fields) -> str:
    lower = {k.lower(): v for k, v in props.items()}
    for f in fields:
        v = lower.get(f.lower())
        if v:
            return v
    return ""


def extract_parts(sch_path: str | Path) -> list[dict]:
    """[{ref, value, mpn, lcsc}] for every placed (non-power) component on the root sheet."""
    data = sexpdata.loads(Path(sch_path).read_text(encoding="utf-8"))
    parts: list[dict] = []
    for node in data[1:] if isinstance(data, list) else []:
        if _head(node) != "symbol":
            continue
        props = _properties(node)
        ref = props.get("Reference", "")
        if not ref or ref.startswith("#"):  # skip power/virtual symbols (#PWR, #FLG)
            continue
        parts.append(
            {
                "ref": ref,
                "value": props.get("Value", ""),
                "mpn": _pick(props, _MPN_FIELDS),
                "lcsc": _pick(props, _LCSC_FIELDS),
            }
        )
    return parts


def check_bom(sch_path: str | Path, timeout: float = 20.0, max_workers: int = 6) -> dict:
    """Check every distinct MPN/LCSC in the schematic on JLCPCB + DigiKey.

    Returns ``{parts: [{part, value, refs, jlcpcb, digikey}], missing_mpn: [{ref, value}]}``.
    ``missing_mpn`` lists components that carry no MPN/LCSC field at all -- an unsourced-part
    gap worth surfacing.
    """
    parts = extract_parts(sch_path)
    uniq: dict[str, dict] = {}
    missing: list[dict] = []
    for p in parts:
        key = (p["mpn"] or p["lcsc"]).strip()
        if not key:
            missing.append({"ref": p["ref"], "value": p["value"]})
            continue
        slot = uniq.setdefault(key.upper(), {"part": key, "value": p["value"], "refs": []})
        slot["refs"].append(p["ref"])

    def _one(info: dict) -> dict:
        res = check_stock(info["part"], timeout=timeout)
        return {
            **info,
            "valid": res["valid"],
            "available_on": res["available_on"],
            "jlcpcb": res["jlcpcb"],
            "digikey": res["digikey"],
        }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        checked = list(ex.map(_one, uniq.values()))
    return {"parts": checked, "missing_mpn": missing}
