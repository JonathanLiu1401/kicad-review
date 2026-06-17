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

## Sourcing parts — find/pull a symbol + footprint without searching online

Don't tell the user to go hunt for a symbol. Source it natively, cheapest tier first:

```
py …\lib\kicad_review_cli.py find-symbol <name>     # search the INSTALLED KiCad libraries (offline)
py …\lib\kicad_review_cli.py pull-part   <MPN>      # pull from online by part number (easyeda2kicad)
```

1. **`find-symbol <name>` first.** Most common parts (R, C, op-amps, common ICs, regulators,
   connectors, MOSFETs, diodes) already ship with KiCad — this is an offline lib lookup, highest
   trust, no network. It returns `Lib:Symbol` ids you can use directly.
2. **`pull-part <MPN>` if it's not local.** Resolves the manufacturer part number → LCSC → and
   converts via `easyeda2kicad` (`pip install easyeda2kicad`) into a `.kicad_sym` + `.kicad_mod` +
   STEP/WRL 3D model. Keyless, no login.
3. **Pulled parts are curated, not verified.** Always sanity-check the pinout and footprint against
   the datasheet before trusting a pulled part. (AI-generating a footprint from a datasheet is the
   *least* trustworthy path — LLMs are poor at footprint geometry — so prefer pull/local.)

(MCP equivalents: `kicad_find_symbol` / `kicad_pull_part`.) Placing a sourced symbol *into* the
schematic (with wiring) is not yet built — hand placement/wiring to the KiCad GUI for now.

## Availability — is a part real, orderable, and in stock?

Before recommending or pulling a part, check it's actually sourceable. Checks **both** JLCPCB/LCSC
and DigiKey and normalizes the result (stock qty, price breaks, status, LCSC#/DigiKey#, package).

```
py …\lib\kicad_review_cli.py check-stock  <MPN|LCSC>          # one part, both distributors
py …\lib\kicad_review_cli.py search-parts "<query>" [--limit N]  # find candidates, stock-ranked (JLCPCB)
py …\lib\kicad_review_cli.py check-bom    <project>           # sweep EVERY MPN in the schematic
```

- **JLCPCB/LCSC is keyless** — works out of the box, live stock/price. Validity is an **exact
  MPN/LCSC match**: JLC's keyword search is fuzzy and returns thousands of rows for a bad query, so
  "results came back" ≠ "the part is real". `check-stock` already enforces the exact match;
  `search-parts` deliberately returns the fuzzy candidate list for discovery.
- **DigiKey needs a free key** (5-min self-serve registration at developer.digikey.com → an app
  with OAuth2 client-credentials). Enable it **either** by the `DIGIKEY_CLIENT_ID` +
  `DIGIKEY_CLIENT_SECRET` env vars **or** a local JSON file at `~/.claude/kicad-review-credentials.json`
  with those two keys (outside the repo, never committed). The **file works for every process
  immediately**; env vars set via `setx` only reach newly-started process trees, so prefer the file.
  Until configured, `check-stock` shows DigiKey as "not configured" and the JLCPCB half still answers.
  - **Use a *Production* app, not Sandbox** — a Sandbox key returns structurally-valid *fake*
    stock/price the code can't tell from real.
  - **Honesty note:** the JLCPCB path is verified against the live endpoint; the DigiKey path is
    implemented to the v4 spec but is first exercised for real when a key is added. If, after
    adding a key, DigiKey shows **all-zero stock / null prices across every part**, that's a v4
    field-mapping mismatch (the normalizer fell through its `.get(...)` defaults), **not** real
    availability — grab one live response body and re-align `normalize_digikey`.
- **Workflow:** `search-parts` to discover → note the `lcsc` code → `pull-part`/`pull_lcsc` to bring
  in the symbol+footprint → `check-stock` to confirm it's in stock before committing. `check-bom`
  flags out-of-stock, invalid, and **unsourced (no-MPN)** components across the whole design at once.

(MCP equivalents: `kicad_check_stock` / `kicad_search_parts` / `kicad_check_bom`.)

## Editing (v1) — guarded, surgical schematic edits

You can now change a component's **Value** or **Footprint association** in the schematic. These
are *surgical, in-place* edits (only the one property string changes; the rest of the file is
byte-identical — no full-file resave, so KiCad-10 constructs are never dropped).

**Always dry-run first, show the diff, get the human's OK, then apply.** Never write the live
schematic blindly.

```
py …\lib\kicad_review_cli.py set-value     <project> <refdes> <value>          # dry run
py …\lib\kicad_review_cli.py set-footprint <project> <refdes> <Lib:Fp>         # dry run
py …\lib\kicad_review_cli.py set-property  <project> <refdes> <name> <value>   # dry run (any field: MPN, LCSC, …)
py …\lib\kicad_review_cli.py place-like     <project> <src-ref> <new-ref> <x> <y>  # dry run
#  …add --apply to write it to the live file (only applied if ERC does not regress)
```

`set-property` generalizes `set-value`/`set-footprint` to **any** existing field (MPN, LCSC, Description, …)
under the same copy→ERC→diff→approve guard — handy for fixing BOM/sourcing fields. (MCP: `kicad_set_property`.)

Workflow:
1. **Dry-run** the edit (no `--apply`). The guard copies the project, makes the edit on the copy,
   re-runs `kicad-cli sch erc`, and returns a unified **diff** + an **ERC error delta**.
2. **Show the diff and the ERC delta to the user** and get explicit approval. If ERC regressed,
   stop — do not apply.
3. **Apply** with `--apply` only after approval. The write is atomic and only happens if ERC did
   not regress.

(MCP equivalents: `kicad_set_value` / `kicad_set_footprint`, both dry-run unless `apply=True`.)

### Place a part (clone an existing one) — `place-like`

`place-like` adds a **new FLOATING symbol** by *cloning a part type already on the board*: it copies
an existing placed instance (its library symbol is already cached, so the result is parse-valid),
mints a fresh UUID + fresh per-pin UUIDs, a new non-colliding refdes, and a grid-snapped position.
Use it to add another instance of something already present — e.g. another decoupling cap or pull-up.

```
py …\lib\kicad_review_cli.py place-like <project> C1 C99 50.8 60.96     # clone C1 -> C99 at (x,y) mm
#  …add --apply to write it (only if the schematic still LOADS in kicad-cli)
```

The placed part is **unwired**: its pins float, so ERC gains the expected `pin_not_connected`
warnings — that increase is normal, so the safety gate here is "the schematic still loads" rather
than "ERC did not regress". **Wiring is geometric and stays a GUI step.** Report the new
unconnected-pin count to the user and tell them to wire + fine-position it in the KiCad editor.
(MCP equivalent: `kicad_place_like`, dry-run unless `apply=True`.)

**Still out of scope (v1):** *auto-wiring* (connectivity is geometric — the GUI or a
connectivity-aware step is needed), placing a *newly-sourced* part type not yet on the board
(needs `lib_symbols` injection — a later increment; for now clone a part type already present),
editing `.kicad_sym`/`.kicad_mod` library geometry, and any PCB layout.

## Fabrication — exports + readiness (read-only)

Produce the deliverables a board shop / assembler needs, and grade whether the board is actually
ready to send out. This is review-agent work — it *packages and assesses* the handoff, it does **not**
author layout.

```
py …\lib\kicad_review_cli.py fab-export <project>   # gerbers + drill + pick-and-place + STEP
py …\lib\kicad_review_cli.py fab-check  <project>   # READY? (DRC + board outline) + the package
```

`fab-check` is the one to lead with: it runs DRC, checks for an Edge.Cuts board outline, and returns a
**ready / not-ready** verdict plus the produced package. `ready` is False on any blocker (DRC errors,
missing outline) — a board with DRC errors is commonly rejected or fabbed with defects, so don't call a
board "done" until `fab-check` is clean. (MCP: `kicad_fab_export` / `kicad_fab_check`.)

## JLCPCB manufacturability — check against authoritative limits (+ apply rules)

If the board is made/assembled at **JLCPCB**, the DRC must match *their* real capabilities, not
KiCad's defaults. Capability values are transcribed + **cited** from JLCPCB's published pages
(`jlcpcb.py` `SOURCES`/`VERIFIED`) — never invented. Re-verify when JLCPCB updates.

```
py …\lib\kicad_review_cli.py jlcpcb-check       <project>     # flag features/rules JLCPCB can't make
py …\lib\kicad_review_cli.py jlcpcb-apply-rules <project>     # dry run; --apply tightens .kicad_pro rules
```

- **`jlcpcb-check`** compares the board (keyed to its layer count + copper weight) to JLCPCB minimums.
  Track width / via drill / annular ring are **measured** from geometry (a `blocker` if JLCPCB
  physically can't make them). Clearance & copper-to-edge are **config** checks — a *rule* looser than
  JLCPCB is a `major`: KiCad's DRC will pass a sub-JLCPCB feature you add later. So "geometry within
  limits" **plus** majors means *makeable as drawn, but your DRC won't protect you* — don't read it as
  all-clear.
- **`jlcpcb-apply-rules`** raises only the looser-than-JLCPCB rules in `.kicad_pro` to JLCPCB minimums
  (`max(current, floor)`, never loosens), surgically + scoped to `design_settings.rules` (other blocks
  carry the same key names — a blind replace would hit the wrong one), under a dry-run→diff→approve→apply
  guard. Then KiCad's own DRC enforces JLCPCB limits.
- **Stackup CHECK + reference (done); WRITE deferred.** `jlcpcb-check` compares the board's stackup
  to JLCPCB's *published standard* (sourced from gsuberland's JLCPCB-impedance-API extraction, cited
  in `jlcpcb.STACKUP_SOURCE`) and prints the exact reference stack to set in KiCad Board Setup. It
  frames this as "matches / doesn't match JLCPCB's published standard," never "what JLCPCB will build"
  (JLCPCB assigns the final stack at order). The auto-**write** is intentionally not done: a board
  often has *no explicit stackup* to surgically update (generating one from scratch is risky), and
  the file reflects rather than dictates JLCPCB's build. Only common configs (e.g. 4L/1.6mm) are
  vendored; for other 4+ layer configs the check says "no reference on file" rather than guessing.

(MCP: `kicad_jlcpcb_check` / `kicad_jlcpcb_apply_rules`.)

## PCB layout — the boundary (do vs advise)

LLMs are unreliable at spatial PCB tasks, so the tool draws a hard line and the assistant must hold it.

**REFUSE + ADVISE only (never perform):** trace routing, component placement/movement, length-tuning,
differential pairs, autorouting, drawing the board outline. For these, don't attempt an edit — explain
the approach and constraints (impedance, return paths, clearance, thermals) and tell the user what to
do in the KiCad GUI. This is the deliberate cutoff: spatial/judgment work stays human-driven with AI
*advice*, not AI edits.

**Deterministic, fully-specified board edits are in-bounds** (explicit net + layer + polygon, guarded
S-expression) — e.g. defining a copper-zone/pour outline. BUT a hard headless limit, **verified
empirically**: `kicad-cli` does **not** fill zones (it plots only cached fills), so an added zone is an
**unfilled outline** — the user must fill it in KiCad (Edit ▸ Fill All Zones / `B`). True headless
filling would need the KiCad GUI or the IPC API, which this tool avoids by design. So a "pour" here =
*define the outline*; KiCad does the fill. Say that plainly; don't imply a finished pour.

Net: review everything; do schematic value/footprint/property edits + clone-place; produce fab +
JLCPCB checks; for PCB **layout**, advise — and at most define a zone outline for the user to fill.

## Scope & roadmap

v0: read + review. **v1 (now): surgical Value/Footprint edits + clone-`place-like` behind a
copy→ERC/load→diff→approve→apply guard (above); local-libs→`easyeda2kicad`-pull part sourcing.**
Next: placing a *newly-sourced* part (lib_symbols injection), then connectivity-aware wiring;
manufacturing-export hard-block.
