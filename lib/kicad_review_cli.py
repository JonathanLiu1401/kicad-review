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
