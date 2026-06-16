# kicad-review v1 — Native Schematic Editing (Design Spec)

**Date:** 2026-06-16
**Status:** Approved (design); pending implementation plan
**Builds on:** v0 read+review (`2026-06-15-kicad-review-plugin-design.md`)

## 1. Goal & scope

Let Claude **edit** KiCad 10 schematics natively (no PCB layout). Approved scope = **Hybrid (C)**:

- ✅ **Edit component Value** — surgical, in-place
- ✅ **Edit Footprint association** (the `Lib:Footprint` string) — surgical, in-place
- ✅ **Edit other instance properties** (MPN, Manufacturer, …) — same mechanism
- ✅ **Place a symbol** (place-but-don't-wire) — behind a KiCad-10 verification gate
- ⚠️ **"Edit symbols" = symbol *instances*** (properties/placement), **not** `.kicad_sym` graphic definitions
- ❌ **Wiring / forming nets** — out of v1 (geometric-connectivity wall; hand to GUI)
- ❌ `.kicad_mod` / `.kicad_sym` definitions, PCB layout — out

## 2. Hard constraints (from research, 2026-06-16)

1. **No schematic editing API in KiCad 9/10/11** (IPC + SWIG are PCB-only; `kicad-python` v0.7.1 PCB-only). All edits write the `.kicad_sch` S-expression directly.
2. **`kicad-sch-api` targets KiCad 9** and its closed parser **silently drops KiCad-10-only tokens on save** → load→edit→save could *corrupt* a real KiCad-10 board. Reserved for Tier 2, gated.
3. **`sexpdata` (already a repo dep) is a *generic* parser** — it round-trips any valid S-expr without dropping tokens. The corruption risk is specific to semantic libraries with closed parsers, not to targeted in-place edits.
4. Connectivity is **geometric** (wires connect only if endpoints land on exact pin coords after the placement→pin transform with rotation+mirror) — which is why wiring is out of v1.

## 3. Architecture — two tiers by risk

**Tier 1 — Surgical edits (safe core).** Locate the placed instance by Reference, replace **only** the one property string, leave the rest of the file **byte-identical**. A *targeted span replacement*, not a full-file resave through a foreign serializer → **zero token-drop risk**. The repo's `sexpdata` reader (`parse.py` nav helpers) locates the exact node/byte span; the write replaces just that span.

Key correctness details (from the real PERIPH.kicad_sch):
- Edit the **placed instance** `(property "Value" …)`, **not** the `lib_symbols` cache (they legitimately differ).
- Match the right instance via its sibling `(property "Reference" "R1")`.
- **Reference is dual-site** — also appears in `(instances … (path … (reference "R1")))`; a *rename* must touch both. (Value/Footprint are single-site.)

**Tier 2 — Place-symbol (gated).** A valid placement needs: fresh symbol UUID + per-pin UUIDs, `instances/path` = the root-sheet UUID (the file's top `(uuid …)`), a `lib_symbols` cache inject (source the symbol from the system `.kicad_sym` libs via the same nav helpers), and a non-colliding refdes. Placed symbols land **floating** (wiring is the user's, in the GUI). Only enabled if the verification gate passes.

## 4. Safety model (every edit)

```
propose → operate on a COPY → render unified diff → re-run `kicad-cli sch erc`
        → human approves → apply to the real file (atomic replace)
```

Never mutate the live `.kicad_sch` directly. Reuses the repo's `run_erc` (and renders) for post-edit validation. **Prerequisite:** the v0 BLOCKER bug (stale-artifact read in `run_erc`) must be fixed first, or the guard could approve a broken edit against a stale ERC result.

## 5. Components (new `kicad_mcp/edit/`)

- `locate.py` — find a placed instance + its property nodes (and byte spans) by Reference; reuse `parse.py` helpers; handle instance-vs-cache and Reference dual-site.
- `surgical.py` — `set_value` / `set_footprint` / `set_property` as targeted span replacements.
- `place.py` — gated symbol placement (UUID/cache/refdes scaffolding).
- `guard.py` — the copy→diff→ERC→approve→apply transaction + unified-diff rendering.
- **Surfaces:** CLI `set-value` / `set-footprint` (+ gated `add-symbol`); MCP `kicad_set_value` etc. (guarded); a `SKILL.md` editing-workflow section.

## 6. De-risking & testing

- **Build task #1 = verification gate:** load→save→diff a *copy* of PERIPH through any candidate full-file writer; assert no KiCad-10 tokens vanish and `sch erc` still passes. Decides Tier-2's path before any code depends on it. (Tier 1 doesn't need it — it never resaves the whole file.)
- **Golden tests on a PERIPH copy:** change a value → assert (a) the value changed, (b) the rest of the file is byte-identical, (c) `sch erc` exit code unchanged, (d) it reopens.

## 7. Build sequence

1. Fix v0 BLOCKER (stale-artifact read) — prerequisite for the guard.
2. Verification gate.
3. `locate` + `surgical` (value/footprint) + `guard`.
4. CLI + MCP surfaces + skill.
5. Golden tests.
6. *(gated)* place-symbol.

## 8. Sources (verified 2026-06-16)

- No schematic API in 9/10/11: https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/for-addon-developers/index.html ; `kicad-python` v0.7.1 https://pypi.org/project/kicad-python/
- `kicad-sch-api` (KiCad-9-targeted, closed parser, bundled MCP; bugs #202/#203/#204): https://github.com/circuit-synth/kicad-sch-api
- `.kicad_sch` / `.kicad_sym` S-expr format: https://dev-docs.kicad.org/en/file-formats/sexpr-schematic/index.html
- Fallbacks: KiCadFiles (MIT, writes sch/sym/mod) https://github.com/Steffen-W/KiCadFiles ; kicad-skip (best pin-coord ergonomics) https://github.com/psychogenic/kicad-skip ; **avoid kiutils** (GPL-3.0 + round-trip corruption issue #120).
