"""MCP tool wrappers for the kicad-review engine.

Thin layer over ``kicad_mcp.review``. The engine does the work; these expose it
to MCP-aware clients. Tools return JSON-serializable dicts/strings — image paths
are returned as text so the client (Claude) can ``Read`` them itself, which is the
whole point of the render-and-Read loop.
"""

from __future__ import annotations

import functools

from fastmcp import FastMCP

from kicad_mcp.review import ReviewEngine
from kicad_mcp.review import kicad as _kicad
from kicad_mcp.review.kicad import KiCadError
from kicad_mcp.review.parse import parse_board, parse_pro


def _parse_currents(currents: dict | None) -> dict:
    out = {}
    for k, v in (currents or {}).items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def _safe(fn):
    """Return a structured ``{"error": ...}`` instead of raising KiCadError, so the
    MCP surface matches the CLI's clean-error contract."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except KiCadError as e:
            return {"error": str(e)}
    return wrapper


def register_review_tools(mcp: FastMCP) -> None:
    """Register the design-review tools with the MCP server."""

    @mcp.tool()
    @_safe
    def kicad_review(project_path: str, scope: str = "all", render: bool = True,
                     currents: dict | None = None) -> dict:
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
            "files": {k: str(v) for k, v in
                      {"sch": proj.sch, "pcb": proj.pcb, "pro": proj.pro}.items() if v},
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
                    "track_widths_mm": dict(sorted(Counter(
                        round(t.width, 3) for t in b.tracks if t.width > 0).items())),
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
