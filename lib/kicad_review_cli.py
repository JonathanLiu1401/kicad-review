#!/usr/bin/env python
"""kicad_review_cli.py -- the Bash/CI entry point for the kicad-review engine.

This is the PRIMARY path the Claude Code skill uses: it works without the MCP
server running and without ``fastmcp`` installed. Every subcommand is a thin
wrapper over ``kicad_mcp.review``.

Usage:
    py lib/kicad_review_cli.py review  <project> [--scope all|schematic|layout]
                                       [--no-render] [--out DIR] [--current NET=AMPS]...
    py lib/kicad_review_cli.py inspect <project>
    py lib/kicad_review_cli.py erc     <project>
    py lib/kicad_review_cli.py drc     <project>
    py lib/kicad_review_cli.py render  <project> [--what all|sch|board|3d]
    py lib/kicad_review_cli.py netlist <project>
    py lib/kicad_review_cli.py version

``<project>`` may be a directory, a .kicad_pro, a .kicad_sch, or a .kicad_pcb.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys

# make the plugin package importable no matter the CWD
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# force UTF-8 stdout so report glyphs (↔, icons) never crash on Windows cp1252
try:
    sys.stdout.reconfigure(encoding="utf-8")  # py3.7+
except Exception:  # pragma: no cover
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from kicad_mcp.review import ReviewEngine, kicad  # noqa: E402
from kicad_mcp.review.parse import parse_board, parse_pro  # noqa: E402


def _parse_currents(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        if "=" in p:
            net, amps = p.split("=", 1)
            with contextlib.suppress(ValueError):
                out[net.strip()] = float(amps)
    return out


def cmd_review(a) -> int:
    eng = ReviewEngine(a.project, out=a.out, current_specs=_parse_currents(a.current))
    pkg = eng.review(scope=a.scope, render=not a.no_render)
    if a.json:
        print(json.dumps({k: v for k, v in pkg.items() if k != "report_markdown"}, indent=2))
        return 0
    print(pkg["report_markdown"])
    print("\n" + "=" * 70)
    print("NEXT STEPS FOR THE REVIEWER (Claude): read each image, then synthesize")
    print("=" * 70)
    print("\nIMAGES TO READ (you can see these; the tools cannot):")
    for im in pkg["images"]:
        print(f"  - {im}")
    if pkg["datasheets"]:
        print("\nDATASHEETS to check layout against:")
        for d in pkg["datasheets"]:
            print(f"  - {d}")
    print("\nRUBRIC:\n" + pkg["rubric"])
    print(f"\nMachine-readable report: {pkg['report_json_path']}")
    return 0


def cmd_inspect(a) -> int:
    proj = kicad.discover_project(a.project)
    print(f"# {proj.name}")
    print(f"dir: {proj.dir}")
    for label, f in (("schematic", proj.sch), ("pcb", proj.pcb), ("project", proj.pro)):
        print(f"{label:10s}: {f if f else '—'}")
    if proj.pro:
        try:
            pro = parse_pro(proj.pro)
            print(f"\nnet classes ({len(pro.net_classes)}):")
            for c in pro.net_classes:
                print(
                    f"  - {c.get('name')}: track={c.get('track_width')} "
                    f"clearance={c.get('clearance')} via={c.get('via_diameter')}/{c.get('via_drill')}"
                )
        except Exception as e:  # noqa: BLE001
            print(f"  (could not read net classes: {e})")
    if proj.pcb:
        try:
            b = parse_board(proj.pcb)
            from collections import Counter

            w = Counter(round(t.width, 3) for t in b.tracks if t.width > 0)
            print(f"\nboard: {b.copper_layers} copper layers, ~{b.copper_oz:.0f} oz outer")
            print(f"  tracks={len(b.tracks)} vias={len(b.vias)} footprints={len(b.footprints)}")
            print(f"  track widths (mm): {dict(sorted(w.items()))}")
        except Exception as e:  # noqa: BLE001
            print(f"  (could not read board: {e})")
    return 0


def cmd_erc(a) -> int:
    proj = kicad.discover_project(a.project)
    erc = kicad.run_erc(proj, a.out)
    viol = []
    for s in erc.get("sheets", []) or []:
        viol += s.get("violations", []) or []
    viol += erc.get("violations", []) or []
    from collections import Counter

    print(f"ERC on {proj.name}: {len(viol)} violations")
    print("by severity:", dict(Counter(v.get("severity") for v in viol)))
    for t, c in Counter(v.get("type") for v in viol).most_common():
        print(f"  {c:4d}  {t}")
    ign = erc.get("ignored_checks", []) or []
    if ign:
        print(f"disabled checks ({len(ign)}):", ", ".join(i.get("key", "?") for i in ign))
    return 0


def cmd_drc(a) -> int:
    proj = kicad.discover_project(a.project)
    drc = kicad.run_drc(proj, a.out, parity=True)
    from collections import Counter

    viol = drc.get("violations", []) or []
    parity = drc.get("schematic_parity", []) or []
    unconn = drc.get("unconnected_items", []) or []
    print(
        f"DRC on {proj.name}: {len(viol)} violations, {len(unconn)} unconnected, "
        f"{len(parity)} parity issues"
    )
    for t, c in Counter(v.get("type") for v in viol).most_common():
        print(f"  {c:4d}  {t}")
    if parity:
        print("schematic-parity:")
        for t, c in Counter(p.get("type") for p in parity).most_common():
            print(f"  {c:4d}  {t}")
    return 0


def cmd_render(a) -> int:
    proj = kicad.discover_project(a.project)
    what = a.what
    made = []
    if what in ("all", "sch") and proj.sch:
        made.append(kicad.render_schematic_pdf(proj, a.out))
    if what in ("all", "board") and proj.pcb:
        for preset in ("front", "back", "copper"):
            made.append(kicad.render_board_pdf(proj, preset, None, a.out))
    if what in ("all", "3d") and proj.pcb:
        made.append(kicad.render_3d(proj, a.out, "top"))
    for m in made:
        print(m)
    return 0


def cmd_netlist(a) -> int:
    proj = kicad.discover_project(a.project)
    print(kicad.export_netlist(proj, a.out))
    return 0


def cmd_version(a) -> int:
    try:
        print("kicad-cli:", kicad.cli_version())
    except Exception as e:  # noqa: BLE001
        print("kicad-cli: NOT FOUND —", e)
    print("kicad-review engine: 0.1.0")
    return 0


def _print_edit(res: dict) -> None:
    verb = "APPLIED" if res["applied"] else "DRY RUN (not written)"
    print(f"{verb}: {res['reference']}.{res['property']}  {res['old']!r} -> {res['new']!r}")
    if res["erc_before"] is not None and res["erc_after"] is not None:
        tail = "  [ERC REGRESSED -- not applied]" if res["erc_regressed"] else ""
        print(f"ERC errors: {res['erc_before']} -> {res['erc_after']}{tail}")
    print("\n--- diff ---")
    print(res["diff"] or "(no textual change)")
    if not res["applied"] and not res["erc_regressed"]:
        print("\nRe-run with --apply to write this change to the live schematic.")


def cmd_set_value(a) -> int:
    from kicad_mcp.edit.guard import propose_edit

    proj = kicad.discover_project(a.project)
    _print_edit(propose_edit(proj, a.reference, "Value", a.value, apply=a.apply))
    return 0


def cmd_set_footprint(a) -> int:
    from kicad_mcp.edit.guard import propose_edit

    proj = kicad.discover_project(a.project)
    _print_edit(propose_edit(proj, a.reference, "Footprint", a.footprint, apply=a.apply))
    return 0


def cmd_place_like(a) -> int:
    from kicad_mcp.edit.guard import propose_place

    proj = kicad.discover_project(a.project)
    res = propose_place(proj, a.source, a.new_ref, (a.x, a.y), apply=a.apply)
    verb = "APPLIED" if res["applied"] else "DRY RUN (not written)"
    print(
        f"{verb}: placed {res['new_ref']} (clone of {res['source_ref']}) "
        f"at ({res['at'][0]}, {res['at'][1]}) mm"
    )
    if res["erc_before"] is not None and res["erc_after"] is not None:
        print(f"ERC errors: {res['erc_before']} -> {res['erc_after']}  (floating pins expected)")
    if not res["loads_ok"]:
        print("WARNING: the cloned schematic failed to load in kicad-cli -- NOT applied.")
    print(f"note: {res['note']}")
    print("\n--- diff ---")
    print(res["diff"] or "(no textual change)")
    if not res["applied"] and res["loads_ok"]:
        print("\nRe-run with --apply to write this placement to the live schematic.")
    return 0


def cmd_find_symbol(a) -> int:
    from kicad_mcp.parts import find_part

    res = find_part(a.query, do_pull=False)
    if res["source"] == "local":
        print(f"Local KiCad libraries — {len(res['symbols'])} symbol hit(s) for {a.query!r}:")
        for s in res["symbols"]:
            print(f"  {s}")
        if res.get("footprints"):
            print(f"footprint hits ({len(res['footprints'])}):")
            for fp in res["footprints"][:20]:
                print(f"  {fp}")
    else:
        print(res["suggestion"])
    return 0


def cmd_pull_part(a) -> int:
    from kicad_mcp.parts import pull as ppull

    res = ppull.pull_mpn(a.mpn, a.out or a.mpn)
    print(f"Pulled {res['mpn']} ({res['lcsc']}):")
    print(f"  symbol:    {res['symbol']}")
    print(f"  footprint: {res['footprint_dir']}")
    print(f"  3D model:  {res['model_dir']}")
    print("\nNote: pulled parts are curated, but verify pinout/footprint against the datasheet.")
    return 0


def _price1(breaks) -> str:
    return f"${breaks[0]['price']}@{breaks[0]['qty']}" if breaks else "—"


def _fmt_jlc(d: dict) -> str:
    if d.get("error"):
        return f"error: {d['error']}"
    if not d.get("found"):
        return "no exact match (not in JLC assembly catalog)"
    state = f"in stock {d['stock']:,}" if d["stock"] > 0 else "OUT OF STOCK"
    return (
        f"{state} | {d['lcsc']} {d['library_type']} | {d['package']} | {_price1(d['price_breaks'])}"
    )


def _fmt_dk(d: dict) -> str:
    if not d.get("configured", True):
        return "not configured (set DIGIKEY_CLIENT_ID/SECRET — free key: developer.digikey.com)"
    if d.get("error"):
        return f"error: {d['error']}"
    if not d.get("found"):
        return "not found"
    state = f"in stock {d['stock']:,}" if d["stock"] > 0 else "OUT OF STOCK"
    return f"{state} | {d['dkpn']} {d.get('status')} | {_price1(d['price_breaks'])}"


def cmd_check_stock(a) -> int:
    from kicad_mcp.parts.stock import check_stock

    r = check_stock(a.mpn)
    verdict = (
        f"VALID — in stock on {', '.join(r['available_on'])}"
        if r["valid"]
        else "NOT available on JLCPCB or DigiKey"
    )
    print(f"{a.mpn}: {verdict}")
    print(f"  JLCPCB:  {_fmt_jlc(r['jlcpcb'])}")
    print(f"  DigiKey: {_fmt_dk(r['digikey'])}")
    return 0


def cmd_search_parts(a) -> int:
    from kicad_mcp.parts.stock import search_jlcpcb

    hits = search_jlcpcb(a.query, limit=a.limit)
    print(f"{a.query!r} — {len(hits)} JLCPCB candidate(s), stock-ranked:")
    for c in hits:
        print(
            f"  {c['lcsc'] or '?':>9}  {c['mpn']:<24} stock {c['stock']:>8,}  "
            f"{c['library_type']:<8} {(c['package'] or ''):<10} {_price1(c['price_breaks'])}"
        )
    return 0


def cmd_check_bom(a) -> int:
    from kicad_mcp.parts.bom import check_bom

    proj = kicad.discover_project(a.project)
    res = check_bom(proj.sch)
    parts = res["parts"]
    print(
        f"{proj.name}: {len(parts)} unique part number(s), "
        f"{len(res['missing_mpn'])} component(s) with no MPN field"
    )
    for p in sorted(parts, key=lambda x: x["part"]):
        jl, dk = p["jlcpcb"], p["digikey"]
        jl_s = f"JLC {jl['stock']:,}" if jl.get("found") else "JLC —"
        if not dk.get("configured", True):
            dk_s = "DK n/c"
        elif dk.get("found"):
            dk_s = f"DK {dk['stock']:,}"
        else:
            dk_s = "DK —"
        flag = "✓" if p["valid"] else "✗"  # valid = in stock on either distributor
        refs = ",".join(p["refs"][:4]) + ("…" if len(p["refs"]) > 4 else "")
        print(f"  {flag} {p['part']:<24} {jl_s:<16} {dk_s:<14} [{refs}]")
    if res["missing_mpn"]:
        print("  no MPN field:", ", ".join(m["ref"] for m in res["missing_mpn"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kicad_review_cli", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("project", help="dir or .kicad_pro/.kicad_sch/.kicad_pcb")
        sp.add_argument("--out", default=None, help="output dir (default <project>/.kicad-review)")

    r = sub.add_parser("review", help="full design review (findings + images + rubric)")
    add_common(r)
    r.add_argument("--scope", choices=["all", "schematic", "layout", "pcb"], default="all")
    r.add_argument("--no-render", action="store_true", help="skip image rendering")
    r.add_argument(
        "--current",
        action="append",
        default=[],
        metavar="NET=AMPS",
        help="expected current per net (repeatable)",
    )
    r.add_argument("--json", action="store_true", help="emit the evidence package as JSON")
    r.set_defaults(func=cmd_review)

    for name, fn, helptext in (
        ("inspect", cmd_inspect, "quick structured project summary"),
        ("erc", cmd_erc, "run ERC and summarize"),
        ("drc", cmd_drc, "run DRC (+ schematic parity) and summarize"),
        ("netlist", cmd_netlist, "export a netlist and print its path"),
    ):
        sp = sub.add_parser(name, help=helptext)
        add_common(sp)
        sp.set_defaults(func=fn)

    rd = sub.add_parser("render", help="render schematic/board/3D images")
    add_common(rd)
    rd.add_argument("--what", choices=["all", "sch", "board", "3d"], default="all")
    rd.set_defaults(func=cmd_render)

    sv = sub.add_parser("set-value", help="surgically set a Value (dry run unless --apply)")
    sv.add_argument("project", help="dir or .kicad_pro/.kicad_sch")
    sv.add_argument("reference", help="component refdes, e.g. R1")
    sv.add_argument("value", help="new Value")
    sv.add_argument("--apply", action="store_true", help="write to the live schematic")
    sv.set_defaults(func=cmd_set_value)

    sf = sub.add_parser(
        "set-footprint", help="surgically set a Footprint association (dry run unless --apply)"
    )
    sf.add_argument("project", help="dir or .kicad_pro/.kicad_sch")
    sf.add_argument("reference", help="component refdes, e.g. R1")
    sf.add_argument("footprint", help="new Lib:Footprint")
    sf.add_argument("--apply", action="store_true", help="write to the live schematic")
    sf.set_defaults(func=cmd_set_footprint)

    pl = sub.add_parser(
        "place-like",
        help="place a new FLOATING symbol by cloning an existing one (dry run unless --apply)",
    )
    pl.add_argument("project", help="dir or .kicad_pro/.kicad_sch")
    pl.add_argument("source", help="refdes of an existing instance to clone, e.g. C1")
    pl.add_argument("new_ref", help="refdes for the new instance, e.g. C99 (must not exist)")
    pl.add_argument("x", type=float, help="X position in mm")
    pl.add_argument("y", type=float, help="Y position in mm")
    pl.add_argument("--apply", action="store_true", help="write to the live schematic")
    pl.set_defaults(func=cmd_place_like)

    fs = sub.add_parser("find-symbol", help="search installed KiCad libraries for a symbol")
    fs.add_argument("query", help="part name or fragment, e.g. LM358")
    fs.set_defaults(func=cmd_find_symbol)

    pp = sub.add_parser(
        "pull-part", help="pull symbol+footprint+3D for an MPN via easyeda2kicad (online)"
    )
    pp.add_argument("mpn", help="manufacturer part number, e.g. DRV8234RTER")
    pp.add_argument("--out", default=None, help="output path prefix (default ./<MPN>)")
    pp.set_defaults(func=cmd_pull_part)

    cs = sub.add_parser(
        "check-stock", help="check an MPN's validity + live stock on JLCPCB and DigiKey"
    )
    cs.add_argument("mpn", help="manufacturer part number or LCSC code, e.g. NE555DR or C7593")
    cs.set_defaults(func=cmd_check_stock)

    sp = sub.add_parser(
        "search-parts", help="search JLCPCB/LCSC for candidate parts (keyless, stock-ranked)"
    )
    sp.add_argument("query", help="free-text part query, e.g. '0.1uF 0402 X7R'")
    sp.add_argument("--limit", type=int, default=10, help="max candidates (default 10)")
    sp.set_defaults(func=cmd_search_parts)

    cb = sub.add_parser(
        "check-bom", help="check every MPN in a schematic's BOM on JLCPCB + DigiKey"
    )
    cb.add_argument("project", help="dir or .kicad_pro/.kicad_sch")
    cb.set_defaults(func=cmd_check_bom)

    v = sub.add_parser("version", help="show kicad-cli + engine versions")
    v.set_defaults(func=cmd_version)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except kicad.KiCadError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - clean message instead of a raw traceback
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
