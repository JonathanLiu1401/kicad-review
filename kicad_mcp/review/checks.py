"""Deterministic engineering checks -> Findings.

Each ``check_*`` takes already-parsed inputs and returns ``list[Finding]``. They
are defensive: a check that hits unexpected data logs an INFO finding rather than
crashing the whole review. The judgment-heavy work (placement quality, datasheet
conformance, SI on specific nets) is intentionally NOT here -- that is the Claude
skill's job, fed by these findings + the rendered images.
"""

from __future__ import annotations

from collections import defaultdict
import math
import re

from .parse import Board, Netlist, ProjectSettings
from .report import Domain, Finding, Severity

# --------------------------------------------------------------------------- #
# net-name classification
# --------------------------------------------------------------------------- #
_POWER_PATTERNS = [
    r"^\+?\d+(\.\d+)?V",  # leading voltage:  12V, +3V3, 5V0, 1.8V
    r"[_\-/]\+?\d+(\.\d+)?V\d?\b",  # delimited voltage: RAW_5V, SYS_3V3, BUS-12V
    r"^V?BAT",
    r"BATTERY",
    r"^VIN",
    r"^VM$",
    r"^VBUS",
    r"^VCC",
    r"^VDD",
    r"^VOUT",
    r"^\+?3V3",
    r"^\+?5V",
    r"^\+?12V",
    r"^\+?1V",
    r"MOTOR",
    r"^OUT\d",
    r"^SW\d?$",
    r"PACK",
    r"^P\+",
    r"^P-",
]
_GROUND_PATTERNS = [r"^GND", r"GNDREF", r"^AGND", r"^DGND", r"^0$", r"GND$"]


def is_ground(name: str) -> bool:
    return any(re.search(p, name or "", re.I) for p in _GROUND_PATTERNS)


def is_power(name: str) -> bool:
    if is_ground(name):
        return True
    return any(re.search(p, name or "", re.I) for p in _POWER_PATTERNS)


# --------------------------------------------------------------------------- #
# IPC-2221 trace-current model
# --------------------------------------------------------------------------- #
_MM_PER_MIL = 0.0254
_MIL_PER_OZ = 1.378  # 1 oz finished copper thickness in mils


def ipc2221_capacity_a(
    width_mm: float, dT_c: float = 10.0, copper_oz: float = 1.0, external: bool = True
) -> float:
    """Max current (A) a trace of ``width_mm`` carries for a ``dT_c`` rise.

    IPC-2221:  I = k * dT^0.44 * A^0.725 ,  A in mils^2, k=0.048 ext / 0.024 int.
    """
    if width_mm <= 0:
        return 0.0
    k = 0.048 if external else 0.024
    width_mils = width_mm / _MM_PER_MIL
    area_mils2 = width_mils * (copper_oz * _MIL_PER_OZ)
    return k * (dT_c**0.44) * (area_mils2**0.725)


def ipc2221_width_mm(
    current_a: float, dT_c: float = 10.0, copper_oz: float = 1.0, external: bool = True
) -> float:
    """Min trace width (mm) to carry ``current_a`` for a ``dT_c`` rise."""
    if current_a <= 0:
        return 0.0
    k = 0.048 if external else 0.024
    area_mils2 = (current_a / (k * dT_c**0.44)) ** (1.0 / 0.725)
    width_mils = area_mils2 / (copper_oz * _MIL_PER_OZ)
    return width_mils * _MM_PER_MIL


# --------------------------------------------------------------------------- #
# ERC / DRC triage (from kicad-cli JSON)
# --------------------------------------------------------------------------- #
def _erc_violations(erc: dict) -> list[dict]:
    out = []
    for s in erc.get("sheets", []) or []:
        out += s.get("violations", []) or []
    out += erc.get("violations", []) or []
    return out


def check_erc(erc: dict) -> list[Finding]:
    findings: list[Finding] = []
    viol = _erc_violations(erc)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for v in viol:
        by_type[v.get("type", "unknown")].append(v)

    for vtype, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        sev_raw = items[0].get("severity", "warning")
        sev = Severity.MAJOR if sev_raw == "error" else Severity.MINOR
        # a few well-known types get a sharper message
        rec = {
            "power_pin_not_driven": "Add a PWR_FLAG to the rail (or mark the driving "
            "pin as power-output). Confirm the rail is actually fed.",
            "lib_symbol_issues": "Re-link/refresh library symbols (schematic symbols differ "
            "from their library). Often cosmetic, but can mask real pin changes.",
            "pin_to_pin": "Two pins are directly connected without a compatible driver — "
            "verify intent (e.g. two outputs shorted).",
            "footprint_link_issues": "Fix the symbol→footprint association.",
        }.get(vtype, "Review each occurrence in Eeschema's ERC panel.")
        sample = items[0].get("description", "")
        loc = {}
        first_items = items[0].get("items") or []
        if first_items:
            loc["at"] = first_items[0].get("description", "")
        findings.append(
            Finding(
                id=f"erc-{vtype}",
                severity=sev,
                domain=Domain.ELECTRICAL,
                title=f"ERC: {vtype} ×{len(items)}",
                detail=f"{sample}" + (f"  (e.g. {loc.get('at')})" if loc.get("at") else ""),
                recommendation=rec,
                evidence=f"kicad-cli sch erc (json), {len(items)} occurrences",
                check="erc",
            )
        )
    return findings


def check_erc_suppressions(pro: ProjectSettings | None, erc: dict) -> list[Finding]:
    findings: list[Finding] = []
    ignored = erc.get("ignored_checks", []) or []
    downgraded = pro.erc_severities if pro else {}
    if ignored:
        keys = sorted({i.get("key", i.get("description", "?")) for i in ignored})
        names = ", ".join(keys)
        findings.append(
            Finding(
                id="erc-ignored",
                severity=Severity.MINOR,
                domain=Domain.HYGIENE,
                title=f"{len(keys)} ERC checks are disabled",
                detail=f"Disabled ERC checks: {names}. Disabled checks hide real issues; "
                "confirm each was disabled deliberately.",
                recommendation="Re-enable in Schematic Setup → Electrical Rules unless there is a "
                "documented reason to ignore.",
                evidence="erc.json ignored_checks",
                check="erc_suppressions",
            )
        )
    at_warning = [k for k, v in downgraded.items() if v == "warning"]
    if len(at_warning) >= 5:
        findings.append(
            Finding(
                id="erc-downgraded",
                severity=Severity.NIT,
                domain=Domain.HYGIENE,
                title=f"{len(at_warning)} ERC rules are at 'warning' severity (won't block)",
                detail="These rules report warnings, not errors, so they won't fail an ERC gate. "
                "Some of these may be KiCad defaults rather than deliberate choices. "
                f"Examples: {', '.join(at_warning[:8])}.",
                recommendation="Confirm connectivity-critical rules are set to 'error'.",
                evidence=".kicad_pro erc.rule_severities",
                check="erc_suppressions",
            )
        )
    return findings


def check_drc(drc: dict) -> list[Finding]:
    findings: list[Finding] = []
    viol = drc.get("violations", []) or []
    unconnected = drc.get("unconnected_items", []) or []
    parity = drc.get("schematic_parity", []) or []

    by_type: dict[str, list[dict]] = defaultdict(list)
    for v in viol:
        by_type[v.get("type", "unknown")].append(v)
    for vtype, items in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        sev_raw = items[0].get("severity", "warning")
        sev = Severity.MAJOR if sev_raw == "error" else Severity.MINOR
        findings.append(
            Finding(
                id=f"drc-{vtype}",
                severity=sev,
                domain=Domain.DFM,
                title=f"DRC: {vtype} ×{len(items)}",
                detail=items[0].get("description", ""),
                recommendation="Resolve in Pcbnew's DRC panel.",
                evidence=f"kicad-cli pcb drc (json), {len(items)} occurrences",
                check="drc",
            )
        )

    if unconnected:
        findings.append(
            Finding(
                id="drc-unconnected",
                severity=Severity.BLOCKER,
                domain=Domain.ELECTRICAL,
                title=f"{len(unconnected)} unconnected ratsnest items",
                detail="Nets in the netlist are not fully routed/connected on the board.",
                recommendation="Route or intentionally exclude each unconnected item.",
                evidence="drc.json unconnected_items",
                check="drc",
            )
        )

    if parity:
        ptypes = defaultdict(int)
        for p in parity:
            ptypes[p.get("type", "unknown")] += 1
        detail = ", ".join(f"{t} ×{c}" for t, c in sorted(ptypes.items(), key=lambda kv: -kv[1]))
        findings.append(
            Finding(
                id="drc-parity",
                severity=Severity.MAJOR,
                domain=Domain.DFM,
                title=f"{len(parity)} schematic↔board parity issues",
                detail=f"The PCB does not match the schematic: {detail}. "
                "Missing footprints / net conflicts mean the board is out of sync.",
                recommendation="Run 'Update PCB from Schematic' (F8) and reconcile every diff "
                "before fabrication.",
                evidence="kicad-cli pcb drc --schematic-parity",
                check="drc_parity",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# net classes
# --------------------------------------------------------------------------- #
def check_net_classes(pro: ProjectSettings | None) -> list[Finding]:
    if not pro:
        return []
    classes = pro.net_classes
    findings: list[Finding] = []
    if len(classes) <= 1:
        dflt = classes[0] if classes else {}
        tw = dflt.get("track_width")
        findings.append(
            Finding(
                id="netclass-single",
                severity=Severity.MAJOR,
                domain=Domain.POWER_THERMAL,
                title="Only one net class ('Default') for the whole board",
                detail=f"Every net shares the Default class (track width {tw} mm). On a board "
                "mixing power/battery/motor nets with signals, this forces power nets to "
                "the same thin geometry as signals and gives DRC nothing to enforce.",
                recommendation="Add dedicated net classes (e.g. Power, GND, Motor) with wider "
                "track widths and assign the high-current nets to them.",
                evidence=".kicad_pro net_settings.classes",
                check="net_classes",
            )
        )
    return findings


# --------------------------------------------------------------------------- #
# trace current capacity (IPC-2221)
# --------------------------------------------------------------------------- #
_MIN_SUSTAINED_LEN = 1.0  # mm; tracks shorter than this are pad-fanout stubs, not the
# current-limiting path, so they don't define a net's width.


def check_trace_currents(
    board: Board,
    current_specs: dict | None = None,
    dT_c: float = 10.0,
    power_nets: set | None = None,
) -> list[Finding]:
    """For each power net, find the thinnest *sustained* track and report its IPC-2221
    capacity.

    With ``current_specs`` ({net_name: amps}) we flag undersized nets hard. Without,
    we surface the capacity of suspiciously thin power nets. ``power_nets`` is an
    optional set of net names already known to be power rails (e.g. from pin types),
    used in addition to the name heuristic so auto-named rails are not missed.

    Short pad-fanout stubs are excluded so a fat main run in parallel with thin stubs
    is not falsely flagged as undersized.
    """
    current_specs = {k.upper(): v for k, v in (current_specs or {}).items()}
    power_names = {n.upper() for n in (power_nets or set())}
    findings: list[Finding] = []

    # thinnest *sustained* track width per net id (ignoring short fanout stubs)
    segs_by_net: dict[int, list] = defaultdict(list)
    for t in board.tracks:
        if t.net < 0 or t.width <= 0:
            continue
        segs_by_net[t.net].append(t)
    min_w: dict[int, float] = {}
    for nid, segs in segs_by_net.items():
        sustained = [t.width for t in segs if t.length >= _MIN_SUSTAINED_LEN]
        min_w[nid] = min(sustained) if sustained else min(t.width for t in segs)

    external = True  # outer-layer assumption; conservative for capacity
    poured = set(getattr(board, "poured_nets", set()))
    for nid, w in sorted(min_w.items(), key=lambda kv: kv[1]):
        name = board.net_name(nid)
        if is_ground(name):
            continue  # ground handled separately
        if not (is_power(name) or name.upper() in power_names):
            continue  # signals not current-limited here
        cap = ipc2221_capacity_a(w, dT_c=dT_c, copper_oz=board.copper_oz, external=external)
        is_poured = name in poured
        spec = current_specs.get(name.upper())
        if spec:
            need = ipc2221_width_mm(spec, dT_c=dT_c, copper_oz=board.copper_oz, external=external)
            if w < need * 0.95:
                if is_poured:
                    # the net has a copper pour; track-only ampacity is not the whole story
                    findings.append(
                        Finding(
                            id=f"trace-{name}",
                            severity=Severity.MINOR,
                            domain=Domain.POWER_THERMAL,
                            title=f"Power net '{name}' has thin tracks ({w:.3f} mm) but is poured",
                            detail=f"'{name}' is carried by a copper zone, so its bulk current goes "
                            f"through the pour, not only the {w:.3f} mm track stubs. IPC-2221 "
                            f"would want ≥{need:.2f} mm of *track* for {spec} A — verify the "
                            "pour coverage and via stitching actually carry the current into/out "
                            "of the pour (necks at pads/vias are the usual bottleneck).",
                            recommendation=f"Check the '{name}' pour width and the via count where the "
                            "current enters/leaves the pour; widen feed tracks if needed.",
                            location={"net": name},
                            evidence="IPC-2221 (.kicad_pcb tracks; net is poured — pour not sized)",
                            check="trace_currents",
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            id=f"trace-{name}",
                            severity=Severity.MAJOR,
                            domain=Domain.POWER_THERMAL,
                            title=f"Power net '{name}' undersized for {spec} A",
                            detail=f"Thinnest sustained track on '{name}' is {w:.3f} mm (~{cap:.1f} A "
                            f"@ {dT_c:.0f} °C rise, {board.copper_oz:.0f} oz). IPC-2221 wants "
                            f"≥{need:.2f} mm for {spec} A. (No copper pour on this net.) "
                            "Confirm this segment is in the main current path, not a "
                            "lower-current branch, before resizing.",
                            recommendation=f"Widen the {spec} A path on '{name}' to ≥{need:.2f} mm "
                            "(or add a copper pour / parallel layers).",
                            location={"net": name},
                            evidence="IPC-2221 (.kicad_pcb track geometry)",
                            check="trace_currents",
                        )
                    )
        elif w <= 0.30 and not is_poured:
            # informational capacity for suspiciously thin, NON-poured power nets
            findings.append(
                Finding(
                    id=f"trace-cap-{name}",
                    severity=Severity.MINOR,
                    domain=Domain.POWER_THERMAL,
                    title=f"Power net '{name}' routed at {w:.3f} mm (~{cap:.1f} A capacity)",
                    detail=f"Thinnest track on power net '{name}' is {w:.3f} mm → ~{cap:.1f} A "
                    f"@ {dT_c:.0f} °C rise ({board.copper_oz:.0f} oz outer), and the net has "
                    "no copper pour. Verify this exceeds the net's real worst-case current.",
                    recommendation="If the expected current is higher, widen the net or assign a "
                    "Power net class. Pass current_specs to get a hard pass/fail.",
                    location={"net": name},
                    evidence="IPC-2221 (.kicad_pcb track geometry)",
                    check="trace_currents",
                )
            )
    return findings


# --------------------------------------------------------------------------- #
# decoupling
# --------------------------------------------------------------------------- #
def check_decoupling(netlist: Netlist, board: Board | None = None) -> list[Finding]:
    """Flag IC power-input nets that lack a bypass capacitor.

    Uses pin *types* from the netlist: a net is a 'power input' for an IC if that
    IC connects to it through a ``power_in`` pin. A bypass cap is a C* on that same
    net. Optionally reports cap→IC distance from board coordinates.
    """
    findings: list[Finding] = []

    # net name -> set of (ref, pin, type)
    net_nodes: dict[str, list[dict]] = {n["name"]: n["nodes"] for n in netlist.nets}
    # ref -> footprint position
    pos = {}
    if board:
        pos = {f.ref: (f.x, f.y) for f in board.footprints}

    # power-input nets per IC
    ic_power_nets: dict[str, set] = defaultdict(set)
    for name, nodes in net_nodes.items():
        if is_ground(name):
            continue
        for nd in nodes:
            if nd["ref"].startswith("U") and "power_in" in (nd["type"] or ""):
                ic_power_nets[nd["ref"]].add(name)

    for ic in sorted(ic_power_nets):
        for net in sorted(ic_power_nets[ic]):
            caps_on_net = [nd["ref"] for nd in net_nodes.get(net, []) if nd["ref"].startswith("C")]
            if not caps_on_net:
                findings.append(
                    Finding(
                        id=f"decap-missing-{ic}-{net}",
                        severity=Severity.MAJOR,
                        domain=Domain.POWER_THERMAL,
                        title=f"{ic}: power input '{net}' has no bypass capacitor",
                        detail=f"No capacitor is connected to {ic}'s power-input net '{net}'.",
                        recommendation=f"Add a 100 nF (+ bulk) decoupling cap on '{net}' close to {ic}.",
                        location={"refdes": ic, "net": net},
                        evidence="netlist pin types",
                        check="decoupling",
                    )
                )
            elif board and ic in pos:
                # nearest cap distance
                dists = []
                for c in caps_on_net:
                    if c in pos:
                        dx, dy = pos[ic][0] - pos[c][0], pos[ic][1] - pos[c][1]
                        dists.append(math.hypot(dx, dy))
                if dists and min(dists) > 5.0:
                    findings.append(
                        Finding(
                            id=f"decap-far-{ic}-{net}",
                            severity=Severity.MINOR,
                            domain=Domain.POWER_THERMAL,
                            title=f"{ic}: bypass cap on '{net}' is {min(dists):.1f} mm away",
                            detail=f"Nearest decoupling cap ({', '.join(caps_on_net)}) on '{net}' is "
                            f"{min(dists):.1f} mm from {ic}. Bypass caps should sit within a "
                            "couple mm of the pin to be effective at high frequency.",
                            recommendation=f"Move a bypass cap on '{net}' adjacent to {ic}'s power pin.",
                            location={"refdes": ic, "net": net},
                            evidence="netlist + board coords",
                            check="decoupling",
                        )
                    )
    return findings


# --------------------------------------------------------------------------- #
# BOM hygiene
# --------------------------------------------------------------------------- #
_PLACEHOLDER_VALUES = {"", "~", "?", "x", "tbd", "dnp", "value", "n/a"}


def check_bom(netlist: Netlist) -> list[Finding]:
    findings: list[Finding] = []
    missing_val = [
        c["ref"]
        for c in netlist.components
        if str(c.get("value", "")).strip().lower() in _PLACEHOLDER_VALUES
    ]
    if missing_val:
        findings.append(
            Finding(
                id="bom-missing-value",
                severity=Severity.MINOR,
                domain=Domain.BOM,
                title=f"{len(missing_val)} parts have a missing/placeholder value",
                detail=f"Refs: {', '.join(sorted(missing_val)[:20])}"
                + (" …" if len(missing_val) > 20 else ""),
                recommendation="Give every part a real value so the BOM is orderable.",
                evidence="netlist components",
                check="bom",
            )
        )
    return findings
