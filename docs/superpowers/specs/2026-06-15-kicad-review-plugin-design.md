# kicad-review — Design Spec

**Date:** 2026-06-15
**Status:** Approved (design); pending implementation plan
**Author:** Jonathan Liu (with Claude Code)
**Topic:** A Claude Code plugin that lets Claude natively understand KiCad schematic & PCB designs and produce engineering design reviews.

---

## 1. Goal & scope

Give Claude Code the ability to **read, understand, and review** KiCad 9/10 designs —
both schematic and layout — and surface prioritized, well-grounded improvement
suggestions. Reviewing is the priority; generating/building designs is explicitly **not**
a goal (LLMs read & review far more reliably than they author EDA designs).

- **v0 (this spec): read + review only.** No writes to design files.
- **v1 (preview, separate spec): guarded human-approved edits.**

**Review domains (all in scope for v0):** ERC/DRC correctness, power & thermal,
signal integrity & routing, BOM/sourcing/DFM, and schematic+layout validation.

**Primary validation target:** the real `PERIPH` board in this repo
(`In-Pipe-Hardware/v0/PERIPH/`) — KiCad 10, format version `20260306`, 85 components
(BQ76920 BMS, DRV8234 motor driver, TPSM33625 buck, MCP4725 DAC, INA219, USB-PD front end).

## 2. Architecture decision

**Approach: fork `lamaalrajih/kicad-mcp`** (MIT, ~469★, active) and repackage it as a
Claude Code **local plugin**, mirroring the existing `ltspice` local plugin's structure.
The forked MCP server is the engine; a new `SKILL.md` is the Claude Code "brain" that
drives it. We add the review intelligence the base lacks.

Rejected alternatives: (A) wiring together existing servers — fragmented, no review
engine; (B) a clean-room plugin mirroring LTspice — more build than necessary given a
mature MIT base exists.

### 2.1 Research basis (adversarially verified — see §9 for citations)

| Capability | Verdict | Adopt |
|---|---|---|
| Headless ERC/DRC/netlist/BOM/SVG/3D | **ADOPT, zero-build** | `kicad-cli` (what every server delegates to) |
| PCB read + analysis MCP base | **FORK (MIT)** | `lamaalrajih/kicad-mcp` |
| Schematic read/parse | **ADOPT (verify on KiCad 10)** | `kicad-sch-api` (MIT, byte-for-byte claim) + `kiutils` |
| Live PCB edits (v1 only) | **WRAP** | `kicad-python` / `kipy` (official IPC, PyPI v0.7.1) |
| Validation-gate / mfg hard-block pattern | **ADOPT pattern** | `kicad-mcp-pro` |
| Guarded human-approved edits | **BUILD (v1)** | nobody ships this |
| EE review heuristics | **BUILD (v0)** | from engineering knowledge + in-repo datasheets |

### 2.2 Hard constraints from the research

1. **Schematic has NO programmatic write API in KiCad 9/10** (IPC is PCB-editor-only).
   Schematic edits (v1) must use S-expression file manipulation, not `kipy`.
2. **SWIG `pcbnew` is deprecated** (removed in KiCad 11). Do not build on it; use IPC for v1.
3. **KiCad 10 (`20260306`) shipped after every candidate library's last release.**
   Round-trip / parse safety on the real board is **unverified** and must be proven first.

## 3. Plugin layout

```
~/.claude/plugins/local/kicad-review/
├── .claude-plugin/plugin.json      # Claude Code plugin manifest (NEW)
├── main.py                         # MCP server entry (forked)
├── kicad_mcp/                      # forked package
│   ├── server.py, config.py, context.py
│   ├── resources/  tools/  prompts/  utils/   # forked
│   └── review/                     # ★ NEW review engine
│       ├── checks/                 # deterministic EE checks (one module per check)
│       ├── render.py               # render-and-Read helpers (wrap kicad-cli)
│       ├── evidence.py             # assembles the evidence package
│       └── report.py               # severity×domain structured findings → md/json
├── skills/kicad-design/SKILL.md    # ★ NEW review workflow primer
├── commands/                       # ★ NEW slash commands
│   ├── review.md  render.md  erc.md  drc.md  inspect.md
├── lib/kicad_review_cli.py         # ★ NEW thin Bash/CI shim over the same code
├── tests/                          # extended: golden tests vs real PERIPH board
└── requirements.txt                # mcp, base deps, kicad-sch-api, kiutils, sexpdata
```

Registered with `claude mcp add kicad -- python …/main.py` (or a project `.mcp.json`).
The `SKILL.md` auto-loads on KiCad-related tasks (trigger keywords: `.kicad_sch`,
`.kicad_pcb`, KiCad, schematic, PCB, layout, DRC, ERC, netlist, footprint).

## 4. The render-and-Read loop

MCP tools cannot see images; Claude can. Tools **render and return image paths**; the
skill instructs Claude to `Read` them — the pattern that makes the LTspice plugin work.

- `render_schematic` → per-sheet SVG/PNG (`kicad-cli sch export svg`)
- `render_board` → per-layer + preset PNGs (copper+silk, copper-only, power-layers)
  (`kicad-cli pcb export svg/pdf`)
- `render_3d` → photoreal 3D PNG (`kicad-cli pcb render`)

This is what makes **layout** review possible (placement, routing, copper pours,
return paths) rather than guessing from coordinates.

## 5. Review engine (two layers)

Mirrors "LTspice gives numbers, Claude interprets."

### 5.1 Layer 1 — deterministic Python checks (`kicad_mcp/review/checks/`)
Hard, reproducible findings:

- **Power/thermal trace sizing (IPC-2221)** — per power/high-current net, compare actual
  min trace width to required width for its current (from `current_specs` input or
  regulator/connector ratings; else flag "specify current"). Catches PERIPH's 0.2 mm
  default on a battery+motor board.
- **Net-class audit** — flag single-`Default`-class designs; recommend dedicated
  power/GND classes. (PERIPH has exactly one `Default` class at 0.2 mm.)
- **Decoupling check** — per IC power pin, verify a bypass cap on that net (100 nF +
  bulk), and from `.kicad_pcb` coordinates measure cap-to-pin distance.
- **ERC/DRC triage** — ingest JSON, group by severity, and **audit suppressed rules**
  (PERIPH downgraded 27 ERC rules to warning/ignore — itself a finding).
- **DFM checks** — min track/clearance/drill/annular ring vs fab capability;
  schematic↔layout parity via `kicad-cli pcb drc --schematic-parity`.
- **BOM/sourcing hygiene** — DNP, missing MPN/value, duplicate/odd footprints.

Each check returns findings of the form:
`{id, severity, domain, location{sheet,refdes,net,xy}, evidence, rationale, recommendation}`.

### 5.2 Layer 2 — LLM reasoning (in `SKILL.md`, over the evidence package)
Claude reads the rendered images, fuses them with Layer-1 findings and the **datasheets
already in this repo** (`driver-bms-datasheets/*.md`: DRV8234, BQ76920, TPSM33625, …),
and produces judgment findings: BMS Kelvin sense routing, buck thermal-pad via pattern,
motor-output copper, 4-layer return paths, etc.

### 5.3 Output
`reviews/<board>-<YYYY-MM-DD>.md` + `.json`. Findings grouped by **severity**
(blocker / major / minor / nit) × **domain** (electrical, power/thermal, SI, DFM, BOM,
schematic-hygiene), each with location, evidence, rationale, recommendation.

## 6. MCP tool surface (v0)

**Kept from fork:** `list_projects` (resource `kicad://projects`), `open_project`,
`extract_netlist`, `analyze_component_density`, `identify_topologies`, `generate_bom`,
`run_drc`, `render_thumbnail`.

**New:** `run_erc`, `drc_parity`, `render_schematic`, `render_board`, `render_3d`,
`board_stats`, `net_classes`, `check_trace_currents`, `check_decoupling`,
`audit_erc_suppressions`, and the orchestrator **`review(project, scope)`** — runs the
checks, renders the images, and returns the **evidence package** (findings + image paths
+ datasheet pointers + rubric) for the skill to turn into the final report. The LLM
reasoning lives in Claude (via the skill), not inside the tool.

## 7. Schematic-vs-PCB split

The code separates a PCB path (IPC/`kipy`, for v1 edits) from a schematic path
(S-expression files), reflecting the no-schematic-write-API constraint. v0 writes
neither; this only sets up v1 cleanly.

## 8. De-risking, testing, build sequence

### 8.1 KiCad-10 verification gate (build task #1)
A harness that proves `kicad-cli` JSON/exports and `kicad-sch-api`/`kiutils` parsing work
on the **real PERIPH files** before anything depends on them. If a library can't handle
the `20260306` format, fall back to `kicad-cli` + `sexpdata` read-only.

### 8.2 Testing (golden tests vs real PERIPH board)
Assert the review catches the known issues (0.2 mm power traces, single net class, 27
suppressed ERC rules), every render yields a valid non-empty image, and ERC/DRC JSON
parses. The board is the fixture — independently verifiable in KiCad.

### 8.3 Build sequence
1. Fork + run base server in Claude Code on KiCad 10.
2. KiCad-10 verification gate (§8.1).
3. Render-and-Read tools (§4).
4. Deterministic review checks (§5.1).
5. `SKILL.md` + `review()` orchestrator + datasheet fusion (§5.2).
6. Golden tests on PERIPH (§8.2).
7. Slash commands + docs.

### 8.4 v1 preview (separate spec)
Guarded propose→diff→approve→apply edits: PCB via `kipy` (IPC commit/undo), schematic via
`kicad-sch-api`, with ERC/DRC re-run after each edit and a manufacturing-export hard-block
(pattern from `kicad-mcp-pro`).

## 9. Sources (verified)

- KiCad APIs & bindings (IPC is the modern surface; SWIG deprecated, removed in 11.0):
  https://dev-docs.kicad.org/en/apis-and-binding/index.html
- IPC API is PCB-editor-only in 9/10 (no schematic API yet):
  https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html
- `kicad-python` (official IPC bindings, PyPI v0.7.1): https://pypi.org/project/kicad-python/
- `lamaalrajih/kicad-mcp` (MIT read/validate base; delegates DRC to kicad-cli):
  https://github.com/lamaalrajih/kicad-mcp
- `circuit-synth/kicad-sch-api` (MIT; reads/writes .kicad_sch, bundled 15-tool MCP):
  https://github.com/circuit-synth/kicad-sch-api
- `kiutils` (S-expression library, KiCad 6+): https://github.com/mvnmgrx/kiutils
- `Kletternaut/kicad-mcp-pro` (validation-gate + mfg hard-block pattern):
  https://github.com/Kletternaut/kicad-mcp-pro
- `Finerestaurant/kicad-mcp-python` (PCB-write reference only — NO license, do not vendor):
  https://github.com/Finerestaurant/kicad-mcp-python
- `kicad-cli` reference (ERC/DRC `--format json`, exports): https://docs.kicad.org/master/en/cli/cli.html
- IPC-2152 trace optimization (heuristic basis):
  https://www.protoexpress.com/blog/how-to-optimize-your-pcb-trace-using-ipc-2152-standard/
- Decoupling/bypass placement (heuristic basis):
  https://resources.altium.com/p/bypass-and-decoupling-capacitor-placement-guidelines

**Caveat:** all surveyed libraries predate KiCad 10; compatibility must be verified
against the real board (§8.1). No surveyed server offers true human-in-the-loop edit
approval — that is a v1 build.
