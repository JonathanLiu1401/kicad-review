"""ReviewEngine -- orchestrates discovery, runners, parsing, and checks.

Produces an *evidence package*: deterministic findings + rendered image paths +
datasheet pointers + a rubric. The package is what the Claude skill consumes to
write the final, prioritized, human-facing review (the skill adds the visual and
datasheet judgment that only an LLM looking at the images can provide).

Every stage is wrapped so a single failing tool (a render, one parser) degrades
to an INFO finding instead of killing the whole review.
"""

from __future__ import annotations

from . import checks, kicad
from .parse import parse_board, parse_netlist, parse_pro
from .report import Domain, Finding, Severity, sort_findings, to_json, to_markdown

_RUBRIC = """\
You now have: deterministic findings (below/in JSON), rendered images, and datasheet
pointers. To finish the review:
1. Read EVERY rendered image (schematic PDF, board PDFs, 3D PNG). You can see them; the
   tools cannot. Judge placement, routing, copper, pours, return paths, connector/edge use.
2. For each major IC, open its datasheet pointer and check the layout against the
   vendor's layout recommendations (thermal pad vias, sense/Kelvin routing, loop area,
   input/output cap placement).
3. Merge your visual+datasheet observations with the deterministic findings. Promote/
   demote severities using judgment. De-duplicate.
4. Emit a single prioritized review: blocker → major → minor → nit, each with a concrete,
   actionable fix and a specific location (refdes / net / area of the board).
Do NOT invent measurements — cite the deterministic findings or what you can see.
"""


class ReviewEngine:
    def __init__(self, project_path, out: str | None = None, current_specs: dict | None = None):
        self.project = kicad.discover_project(project_path)
        self.out = out
        self.current_specs = current_specs or {}
        self.workdir = kicad.workdir(self.project, out)
        self._findings: list[Finding] = []

    # -- helpers ---------------------------------------------------------- #
    def _stage(self, label: str, fn):
        """Run a stage, capturing failures as INFO findings instead of raising."""
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - deliberate: never let one stage kill the review
            self._findings.append(
                Finding(
                    id=f"stage-fail-{label}",
                    severity=Severity.INFO,
                    domain=Domain.HYGIENE,
                    title=f"Could not complete '{label}'",
                    detail=f"{type(e).__name__}: {e}",
                    recommendation="Run the underlying kicad-cli command manually to diagnose.",
                    evidence="engine stage",
                    check="engine",
                )
            )
            return None

    # -- datasheet discovery --------------------------------------------- #
    def _find_datasheets(self) -> list[str]:
        hits: list[str] = []
        root = self.project.dir
        for up in [root, *root.parents[:4]]:
            for d in up.glob("*datasheet*"):
                if d.is_dir():
                    hits += [str(p) for p in d.glob("*") if p.suffix.lower() in (".md", ".pdf")]
            for d in up.glob("*Datasheet*"):
                if d.is_dir():
                    hits += [str(p) for p in d.glob("*") if p.suffix.lower() in (".md", ".pdf")]
        # de-dup, cap
        seen, out = set(), []
        for h in hits:
            if h not in seen:
                seen.add(h)
                out.append(h)
        return out[:40]

    # -- main ------------------------------------------------------------- #
    VALID_SCOPES = ("all", "schematic", "layout", "pcb")

    def review(
        self, scope: str = "all", render: bool = True, timeout: int = kicad.DEFAULT_TIMEOUT
    ) -> dict:
        if scope not in self.VALID_SCOPES:
            raise kicad.KiCadError(f"unknown scope {scope!r}; expected one of {self.VALID_SCOPES}")
        proj = self.project
        do_sch = scope in ("all", "schematic") and proj.sch is not None
        do_pcb = scope in ("all", "layout", "pcb") and proj.pcb is not None

        pro = self._stage("parse .kicad_pro", lambda: parse_pro(proj.pro)) if proj.pro else None

        # --- schematic side ---
        erc = None
        netlist = None
        if do_sch:
            erc = self._stage("ERC", lambda: kicad.run_erc(proj, self.out, timeout))
            if erc is not None:
                self._findings += self._stage("ERC triage", lambda: checks.check_erc(erc)) or []
                self._findings += (
                    self._stage(
                        "ERC suppression audit", lambda: checks.check_erc_suppressions(pro, erc)
                    )
                    or []
                )
            netpath = self._stage(
                "netlist export", lambda: kicad.export_netlist(proj, self.out, timeout)
            )
            if netpath:
                netlist = self._stage("parse netlist", lambda: parse_netlist(netpath))

        # --- board side ---
        drc = None
        board = None
        if do_pcb:
            drc = self._stage("DRC", lambda: kicad.run_drc(proj, self.out, True, timeout))
            if drc is not None:
                self._findings += self._stage("DRC triage", lambda: checks.check_drc(drc)) or []
            board = self._stage("parse .kicad_pcb", lambda: parse_board(proj.pcb))

        # --- cross-cutting checks ---
        self._findings += (
            self._stage("net-class audit", lambda: checks.check_net_classes(pro)) or []
        )
        if board is not None:
            # nets that touch a power pin (any layer) — authoritative power rails,
            # used in addition to the name heuristic so auto-named rails aren't missed.
            power_nets = set()
            if netlist is not None:
                for n in netlist.nets:
                    if any("power" in (nd.get("type") or "") for nd in n["nodes"]):
                        power_nets.add(n["name"])
            self._findings += (
                self._stage(
                    "trace currents",
                    lambda: checks.check_trace_currents(
                        board, self.current_specs, power_nets=power_nets
                    ),
                )
                or []
            )
        if netlist is not None:
            self._findings += (
                self._stage("decoupling", lambda: checks.check_decoupling(netlist, board)) or []
            )
            self._findings += self._stage("BOM hygiene", lambda: checks.check_bom(netlist)) or []

        # --- renders (the images the skill will Read) ---
        images: list[str] = []
        if render:
            if do_sch:
                p = self._stage(
                    "render schematic", lambda: kicad.render_schematic_pdf(proj, self.out, timeout)
                )
                if p:
                    images.append(str(p))
            if do_pcb:
                for preset in ("front", "back", "copper"):
                    p = self._stage(
                        f"render board {preset}",
                        lambda preset=preset: kicad.render_board_pdf(
                            proj, preset, None, self.out, timeout
                        ),
                    )
                    if p:
                        images.append(str(p))
                p3d = self._stage(
                    "render 3D", lambda: kicad.render_3d(proj, self.out, "top", timeout)
                )
                if p3d:
                    images.append(str(p3d))

        # --- assemble + write ---
        kver = (
            (erc or {}).get("kicad_version")
            or (drc or {}).get("kicad_version")
            or self._stage("cli version", lambda: kicad.cli_version())
            or "?"
        )
        meta = {
            "project": proj.name,
            "kicad_version": kver,
            "files": {
                k: str(v)
                for k, v in {"sch": proj.sch, "pcb": proj.pcb, "pro": proj.pro}.items()
                if v
            },
            "scope": scope,
        }
        findings = sort_findings(self._findings)
        report_md = to_markdown(findings, meta)
        report_json = to_json(findings, meta)
        # write defensively: a write failure must NOT discard the completed review
        md_path = self.workdir / f"review-{proj.name}.md"
        json_path = self.workdir / f"review-{proj.name}.json"
        try:
            md_path.write_text(report_md, encoding="utf-8")
            json_path.write_text(report_json, encoding="utf-8")
        except OSError as e:
            md_path = json_path = None  # type: ignore[assignment]
            report_md += f"\n\n> note: could not write report files ({e})\n"

        return {
            "meta": meta,
            "findings": [f.to_dict() for f in findings],
            "images": images,
            "datasheets": self._find_datasheets(),
            "rubric": _RUBRIC,
            "report_markdown_path": str(md_path) if md_path else None,
            "report_json_path": str(json_path) if json_path else None,
            "report_markdown": report_md,
        }
