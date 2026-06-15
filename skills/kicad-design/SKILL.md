---
name: kicad-design
description: Use when reading, understanding, or reviewing KiCad designs — any .kicad_pro / .kicad_sch / .kicad_pcb file, or tasks involving schematic review, PCB layout review, ERC/DRC, netlists, footprints, power/thermal/trace-width/decoupling/DFM analysis, or "review my board / schematic". Read-and-review focused (v0 makes no edits).
---

# KiCad Design Review

A workflow for reading and **reviewing** KiCad 9/10 schematics and PCBs. It wraps
`kicad-cli` (ERC/DRC/netlist/render) and a deterministic engineering-check engine,
then hands you rendered images and datasheet pointers so you can finish the review
with judgment only you can provide.

**This is review-focused. v0 does not edit design files.** Suggest changes; the human
applies them in KiCad. (Guarded human-approved edits are a planned v1.)

## When to activate

- Any `.kicad_pro`, `.kicad_sch`, or `.kicad_pcb` file is mentioned or present.
- The user says KiCad, schematic review, PCB/layout review, ERC, DRC, netlist,
  footprint, decoupling, trace width, power/thermal, signal integrity, DFM, BOM.
- "Review my board / schematic", "is this layout ok", "check this PCB".

## CLI invocation (primary path)

Always drive the engine through Python:

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py <subcommand> <project>
```

`<project>` is a directory or any `.kicad_pro` / `.kicad_sch` / `.kicad_pcb`. The engine
auto-detects the newest installed `kicad-cli`; override with the `KICAD_CLI_PATH` env var.

| Subcommand | Purpose |
|---|---|
| `inspect <project>` | Fast structured summary — files, net classes, layers, track widths, counts. Run this FIRST. |
| `review <project> [--scope all\|schematic\|layout] [--no-render] [--current NET=AMPS]` | Full review: runs all checks, renders images, prints findings + the list of images to Read + datasheet pointers + a rubric. |
| `erc <project>` | Run ERC, summarize violations + disabled checks. |
| `drc <project>` | Run DRC (+ schematic↔board parity), summarize. |
| `render <project> [--what all\|sch\|board\|3d]` | Render images only; prints their paths. |
| `netlist <project>` | Export a netlist; print its path. |
| `version` | Show kicad-cli + engine versions. |

## Standard review workflow

When asked to review a KiCad design:

1. **Inspect first.** `inspect <project>` — learn the files, net classes, layer count,
   copper weight, and track-width distribution. Never guess what's on the board.
2. **Run the full review.** `review <project>`. This prints a deterministic findings
   report (ERC/DRC triage, schematic↔board parity, net-class audit, IPC-2221 trace-current
   capacity, decoupling coverage + placement distance, BOM hygiene) and ends with a list of
   rendered images and datasheet pointers.
3. **READ EVERY RENDERED IMAGE.** This is the most important step. The tools cannot see;
   **you can.** `Read` each PDF/PNG the review lists:
   - schematic PDF — topology, missing flags, net naming, obvious errors;
   - board front/back/copper PDFs — placement, routing, pours, return paths, thin necks,
     connector/edge use;
   - 3D PNG — overall placement sanity, mechanical/connector layout.
   Layout review is impossible from numbers alone. The thin-trace and decoupling findings
   only become actionable once you can see *where* on the board they are.
4. **Check the datasheets.** For each major IC, open its datasheet pointer (the review lists
   them — e.g. driver/BMS/buck PDFs) and compare the layout to the vendor's layout
   recommendations: thermal-pad via patterns, sense/Kelvin routing, input/output cap
   placement, loop area.
5. **Synthesize one prioritized review.** Merge the deterministic findings with your visual
   and datasheet observations. Promote/demote severities with judgment, de-duplicate, and
   present: **blocker → major → minor → nit**, each with a specific location (refdes / net /
   board area) and a concrete fix. Reference the deterministic finding ids or what you can
   see — never invent a measurement.

## The two layers (why this split exists)

- **Deterministic layer (the engine):** exact, reproducible facts — ERC/DRC counts, parity
  issues, trace widths and their IPC-2221 current capacity, cap-to-pin distances, net classes.
  Trust these numbers.
- **Judgment layer (you):** placement quality, datasheet conformance, signal-integrity
  reasoning on specific nets, whether a thin neck is a real bottleneck or a short stub. This
  needs the rendered images + datasheets. This is your job; the engine cannot do it.

It mirrors LTspice review: the tool gives numbers, you interpret what they mean.

## Power/thermal: passing expected currents

`check_trace_currents` reports each power net's *capacity*. To get a hard pass/fail, pass the
expected current per net (use the EXACT net name from `inspect`/`review`):

```
review <project> --current "12V=4.0" --current "Motor 1 Out 1=3.5"
```

Without `--current`, the engine flags suspiciously thin power nets (≤0.30 mm) for you to
sanity-check against the real current.

## Visualization rule

**Always `Read` the rendered images before reporting on layout.** A board PDF or 3D PNG shows
placement, routing congestion, pour coverage, thin necks, and return-path problems instantly;
the netlist and width numbers hide all of that. Render + Read is the primary path to any
layout finding — coordinates alone are not enough.

## What NOT to do

- **Don't guess at the layout or draw ASCII schematics.** Run `inspect`/`render` and `Read`
  the real images.
- **Don't fabricate measurements.** Every numeric claim must come from a deterministic finding
  or something visible in a rendered image. No invented trace widths, currents, or distances.
- **Don't edit the design files.** v0 is read-only. Recommend changes; the human applies them.
- **Don't skip the renders.** A review that only parrots ERC/DRC counts without looking at the
  board is half a review.
- **Don't trust a stale render.** If KiCad is open with unsaved edits (a `.lck` /
  `_autosave-*` file is present), `kicad-cli` reads the on-disk file, which may lag the editor.
  Note this if something looks inconsistent; ask the user to save.

## Common gotchas

- **Schematic↔board parity issues mean the PCB is out of sync** with the schematic (missing
  footprints, net conflicts). Treat a nonzero parity count as a major finding — fix with
  "Update PCB from Schematic" (F8) before fabrication.
- **A single 'Default' net class** means power/battery/motor nets share signal geometry and
  DRC enforces nothing useful on them. Recommend dedicated Power/GND/Motor classes.
- **`lib_symbol_issues` / `lib_footprint_mismatch`** are usually library drift (cosmetic) but
  can mask real pin/footprint changes — skim a few before dismissing.
- **Net names matter for `--current`.** "12V" ≠ "+12V". Copy the exact name from the report.

## Scope & roadmap

v0 (now): read + review only. v1 (planned): guarded, human-approved edits — propose → diff →
approve → apply, with ERC/DRC re-run after each edit and a manufacturing-export hard-block.
