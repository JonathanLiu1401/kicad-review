"""MCP tool wrappers for the kicad-review engine.

Thin layer over ``kicad_mcp.review``. The engine does the work; these expose it
to MCP-aware clients. Tools return JSON-serializable dicts/strings — image paths
are returned as text so the client (Claude) can ``Read`` them itself, which is the
whole point of the render-and-Read loop.
"""

from __future__ import annotations

import contextlib
import functools

from fastmcp import FastMCP

from kicad_mcp.review import ReviewEngine
from kicad_mcp.review import kicad as _kicad
from kicad_mcp.review.parse import parse_board, parse_pro


def _parse_currents(currents: dict | None) -> dict:
    out = {}
    for k, v in (currents or {}).items():
        with contextlib.suppress(TypeError, ValueError):
            out[k] = float(v)
    return out


def _safe(fn):
    """Return a structured ``{"error": ...}`` for ANY failure, so the MCP surface
    matches the CLI's clean-error contract (not just KiCadError)."""

    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 - every failure -> structured error, like the CLI
            return {"error": f"{type(e).__name__}: {e}"}

    return wrapper


def register_review_tools(mcp: FastMCP) -> None:
    """Register the design-review tools with the MCP server."""

    @mcp.tool()
    @_safe
    def kicad_review(
        project_path: str, scope: str = "all", render: bool = True, currents: dict | None = None
    ) -> dict:
        """Run a full KiCad design review (read-only).

        Runs ERC/DRC (+ schematic↔board parity), audits net classes, computes
        IPC-2221 trace-current capacity, checks decoupling coverage/placement and
        BOM hygiene, and renders the schematic + board + 3D images.

        Returns an evidence package: deterministic ``findings``, ``images`` (paths
        for YOU to Read — the tool cannot see them), ``datasheets`` pointers, a
        ``rubric``, and the markdown ``report``. After calling this you MUST Read
        every image and check the datasheets, then synthesize a prioritized review.

        Args:
            project_path: dir or .kicad_pro / .kicad_sch / .kicad_pcb
            scope: "all", "schematic", "layout", or "pcb"
            render: render images (set False for a fast text-only pass)
            currents: optional {net_name: amps} for hard trace-width pass/fail
        """
        eng = ReviewEngine(project_path, current_specs=_parse_currents(currents))
        pkg = eng.review(scope=scope, render=render)
        # rename for clarity at the MCP boundary
        pkg["report"] = pkg.pop("report_markdown")
        return pkg

    @mcp.tool()
    @_safe
    def kicad_inspect(project_path: str) -> dict:
        """Fast structured summary of a KiCad project: files, net classes, copper
        layer count/weight, track-width distribution, and counts. Run this first."""
        proj = _kicad.discover_project(project_path)
        info: dict = {
            "name": proj.name,
            "dir": str(proj.dir),
            "files": {
                k: str(v)
                for k, v in {"sch": proj.sch, "pcb": proj.pcb, "pro": proj.pro}.items()
                if v
            },
        }
        if proj.pro:
            try:
                pro = parse_pro(proj.pro)
                info["net_classes"] = pro.net_classes
            except Exception as e:  # noqa: BLE001
                info["net_classes_error"] = str(e)
        if proj.pcb:
            try:
                from collections import Counter

                b = parse_board(proj.pcb)
                info["board"] = {
                    "copper_layers": b.copper_layers,
                    "copper_oz": b.copper_oz,
                    "tracks": len(b.tracks),
                    "vias": len(b.vias),
                    "footprints": len(b.footprints),
                    "track_widths_mm": dict(
                        sorted(Counter(round(t.width, 3) for t in b.tracks if t.width > 0).items())
                    ),
                }
            except Exception as e:  # noqa: BLE001
                info["board_error"] = str(e)
        return info

    @mcp.tool()
    @_safe
    def kicad_erc(project_path: str) -> dict:
        """Run ERC headlessly and return the raw kicad-cli JSON (violations +
        ignored_checks)."""
        proj = _kicad.discover_project(project_path)
        return _kicad.run_erc(proj)

    @mcp.tool()
    @_safe
    def kicad_drc(project_path: str) -> dict:
        """Run DRC (with schematic↔board parity) headlessly and return the raw
        kicad-cli JSON (violations + unconnected_items + schematic_parity)."""
        proj = _kicad.discover_project(project_path)
        return _kicad.run_drc(proj, parity=True)

    @mcp.tool()
    @_safe
    def kicad_render(project_path: str, what: str = "all") -> dict:
        """Render images and return their paths for you to Read.

        Args:
            project_path: dir or KiCad file
            what: "all", "sch", "board", or "3d"
        """
        proj = _kicad.discover_project(project_path)
        images: list[str] = []
        if what in ("all", "sch") and proj.sch:
            images.append(str(_kicad.render_schematic_pdf(proj)))
        if what in ("all", "board") and proj.pcb:
            for preset in ("front", "back", "copper"):
                images.append(str(_kicad.render_board_pdf(proj, preset)))
        if what in ("all", "3d") and proj.pcb:
            images.append(str(_kicad.render_3d(proj)))
        return {"images": images, "note": "Read each image — they cannot be seen by the tool."}

    @mcp.tool()
    @_safe
    def kicad_set_value(project_path: str, reference: str, value: str, apply: bool = False) -> dict:
        """Surgically set a component's Value in the schematic, in place (byte-clean —
        only that one property string changes; no full-file resave, so KiCad-10 tokens
        are never dropped).

        DRY RUN by default (apply=False): returns a unified ``diff`` + ERC error delta
        and does NOT touch the live file. Show the diff to the human, get approval, then
        call again with apply=True (it writes only if ERC did not regress).
        """
        from kicad_mcp.edit.guard import propose_edit

        proj = _kicad.discover_project(project_path)
        return propose_edit(proj, reference, "Value", value, apply=apply)

    @mcp.tool()
    @_safe
    def kicad_set_footprint(
        project_path: str, reference: str, footprint: str, apply: bool = False
    ) -> dict:
        """Surgically set a component's Footprint association (the ``Lib:Footprint``
        string) in the schematic. DRY RUN by default; review the ``diff`` with the human,
        then call with apply=True (writes only if ERC did not regress)."""
        from kicad_mcp.edit.guard import propose_edit

        proj = _kicad.discover_project(project_path)
        return propose_edit(proj, reference, "Footprint", footprint, apply=apply)

    @mcp.tool()
    @_safe
    def kicad_place_like(
        project_path: str,
        source_ref: str,
        new_ref: str,
        x: float,
        y: float,
        apply: bool = False,
    ) -> dict:
        """Place a new FLOATING symbol into the schematic by cloning an existing instance.

        Copies the placed ``source_ref`` block (its library symbol is already cached, so the
        result is parse-valid), gives it a fresh UUID + every pin a fresh UUID, the new
        ``new_ref`` refdes, and position ``(x, y)`` mm. The new part is UNWIRED — wiring is
        geometric and stays a GUI step. The reported ERC increase is the expected
        unconnected-pin warnings, so the safety gate is "the schematic still LOADS", not "ERC
        did not regress".

        DRY RUN by default (apply=False): returns the unified ``diff``, ERC delta, and
        ``loads_ok``; does NOT touch the live file. Review with the human, then call with
        apply=True (writes only if ``loads_ok``). Use to add another instance of a part TYPE
        already on the board (e.g. another decoupling cap or pull-up).
        """
        from kicad_mcp.edit.guard import propose_place

        proj = _kicad.discover_project(project_path)
        return propose_place(proj, source_ref, new_ref, (x, y), apply=apply)

    @mcp.tool()
    @_safe
    def kicad_find_symbol(query: str) -> dict:
        """Search the INSTALLED KiCad libraries for a symbol by name/fragment (offline,
        no network). Returns {source:"local", symbols:["Lib:Name", ...]} when found, else
        {source:"not_found", suggestion:...}. TRY THIS FIRST — most common parts (R, C, op-amps,
        common ICs, connectors) already ship with KiCad, so no online search is needed."""
        from kicad_mcp.parts import find_part

        return find_part(query, do_pull=False)

    @mcp.tool()
    @_safe
    def kicad_pull_part(mpn: str, out_dir: str | None = None) -> dict:
        """Pull a part's KiCad symbol + footprint + 3D model from online by manufacturer
        part number (resolves MPN→LCSC via jlcsearch, converts via easyeda2kicad). Returns
        the generated file paths. Use when kicad_find_symbol reports not_found. Requires
        ``easyeda2kicad`` (``pip install easyeda2kicad``). Pulled parts are curated but should
        be verified (pinout/footprint) against the datasheet before trusting them."""
        from kicad_mcp.parts import pull as ppull

        return ppull.pull_mpn(mpn, out_dir or mpn)

    @mcp.tool()
    @_safe
    def kicad_check_stock(mpn: str) -> dict:
        """Check an MPN (or LCSC code) for validity + LIVE stock on BOTH distributors at once.

        Returns ``{mpn, jlcpcb:{...}, digikey:{...}}`` with, per source: found/valid, stock
        quantity, price breaks, status, LCSC#/DigiKey#, package, and datasheet. JLCPCB/LCSC is
        keyless; DigiKey needs ``DIGIKEY_CLIENT_ID``/``DIGIKEY_CLIENT_SECRET`` env (free key at
        developer.digikey.com) and reports ``configured: False`` until set — the JLCPCB half
        still works regardless. NOTE: JLCPCB validity is an EXACT MPN/LCSC match (its keyword
        search is fuzzy, so a non-empty result is NOT proof the part exists)."""
        from kicad_mcp.parts.stock import check_stock

        return check_stock(mpn)

    @mcp.tool()
    @_safe
    def kicad_search_parts(query: str, limit: int = 10) -> dict:
        """Search JLCPCB/LCSC (keyless) for candidate parts by free-text query (e.g.
        "0.1uF 0402 X7R" or a partial MPN), stock-ranked. Returns ``{query, candidates:[{lcsc,
        mpn, manufacturer, stock, library_type, package, price_breaks, datasheet, url}]}``. Feed a
        candidate's ``lcsc`` code to kicad_pull_part to pull its symbol + footprint."""
        from kicad_mcp.parts.stock import search_jlcpcb

        return {"query": query, "candidates": search_jlcpcb(query, limit=limit)}

    @mcp.tool()
    @_safe
    def kicad_check_bom(project_path: str) -> dict:
        """Sweep a whole schematic's sourcing: extract every component's MPN/LCSC and check each
        on JLCPCB + DigiKey in parallel. Returns ``{parts:[{part, value, refs, jlcpcb, digikey}],
        missing_mpn:[{ref, value}]}`` — surfacing out-of-stock, invalid, or unsourced (no-MPN)
        parts across the entire design in one call."""
        from kicad_mcp.parts.bom import check_bom

        proj = _kicad.discover_project(project_path)
        return check_bom(proj.sch)

    @mcp.tool()
    @_safe
    def kicad_fab_export(project_path: str, out_dir: str | None = None) -> dict:
        """Export the fabrication package via kicad-cli (read-only): Gerbers, Excellon drill,
        pick-and-place position file, and a STEP 3D model. Returns the path of each deliverable.
        Use for a board handoff to a fab/assembler. (This is an export, not layout authoring.)"""
        from kicad_mcp.review import fab

        proj = _kicad.discover_project(project_path)
        return fab.export_fab_package(proj, out_dir)

    @mcp.tool()
    @_safe
    def kicad_fab_check(project_path: str, out_dir: str | None = None) -> dict:
        """Grade whether a board is READY to fabricate AND produce the fab package. Returns
        ``{ready, drc_errors, findings:[{severity,title,detail}], package:{gerbers, drill,
        pick_and_place, step}}``. ``ready`` is False on any blocker (DRC errors, or no Edge.Cuts
        board outline). A review verdict over the fab handoff, not an edit."""
        from kicad_mcp.review import fab

        proj = _kicad.discover_project(project_path)
        return fab.check_fab_readiness(proj, out_dir)

    @mcp.tool()
    @_safe
    def kicad_set_property(
        project_path: str, reference: str, property: str, value: str, apply: bool = False
    ) -> dict:
        """Surgically set ANY property (e.g. MPN, LCSC, Description) of a placed component in the
        schematic, in place (byte-clean -- only that property string changes). Generalizes
        kicad_set_value/kicad_set_footprint. DRY RUN by default; review the ``diff``, then call
        with apply=True (writes only if ERC did not regress). Updates an EXISTING property."""
        from kicad_mcp.edit.guard import propose_edit

        proj = _kicad.discover_project(project_path)
        return propose_edit(proj, reference, property, value, apply=apply)

    @mcp.tool()
    @_safe
    def kicad_jlcpcb_check(project_path: str) -> dict:
        """Check a board against JLCPCB's AUTHORITATIVE published capabilities (cited in
        ``sources``/``verified`` — not hallucinated), keyed to its layer count + copper weight.

        Returns ``{manufacturable, layers, copper_oz, thickness_mm, limits, findings, sources,
        verified}``. IMPORTANT: ``manufacturable`` reflects GEOMETRY only (track width, via drill,
        annular ring are MEASURED from the board). The 'major' findings are CONFIG checks — design
        rules looser than JLCPCB (clearance, copper-to-edge) that KiCad's DRC won't catch — NOT
        measured geometric violations. So manufacturable=True with majors means "JLCPCB can make
        what's drawn, but your DRC won't protect you from adding a sub-JLCPCB feature.\" """
        from kicad_mcp.review import jlcpcb

        return jlcpcb.check_jlcpcb_manufacturability(_kicad.discover_project(project_path))

    @mcp.tool()
    @_safe
    def kicad_jlcpcb_apply_rules(project_path: str, apply: bool = False) -> dict:
        """Raise the board's ``.kicad_pro`` design rules to JLCPCB's authoritative minimums so
        KiCad's own DRC enforces what JLCPCB can make. Only RAISES looser-than-JLCPCB rules
        (``max(current, JLCPCB-floor)``); never loosens. Surgical + scoped to design_settings.rules.

        DRY RUN by default: returns ``{changes:[{rule,old,new}], diff, applied, sources, verified}``
        and does NOT touch the file. Review the diff, then call with apply=True (writes only if the
        result is valid JSON that round-trips to the intended values)."""
        from kicad_mcp.edit.board_rules import propose_jlcpcb_rules

        return propose_jlcpcb_rules(_kicad.discover_project(project_path), apply=apply)

    @mcp.tool()
    @_safe
    def kicad_add_zone(
        project_path: str,
        net: str,
        layer: str,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        apply: bool = False,
    ) -> dict:
        """Add a copper-zone (pour) OUTLINE to the PCB over a rectangle, for an explicit ``net`` +
        copper ``layer``. This is a basic, fully-specified board op -- routing and component
        placement are OUT (advice-only; see the skill).

        IMPORTANT: kicad-cli CANNOT fill zones, so this writes an UNFILLED outline -- the user must
        fill it in KiCad (Edit > Fill All Zones / 'B'). The ``net`` must exist in the board's net
        table (clear error otherwise; use "" for the no-net zone). DRY RUN by default: returns
        ``{net, net_num, layer, points, diff, loads_ok, applied, note}`` and does NOT touch the file;
        the live board changes only when apply=True and the edited board still loads in kicad-cli."""
        from kicad_mcp.edit.zones import propose_zone, rect_points

        proj = _kicad.discover_project(project_path)
        return propose_zone(proj, net, layer, rect_points(x1, y1, x2, y2), apply=apply)
