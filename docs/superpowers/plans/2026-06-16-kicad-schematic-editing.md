# kicad-review v1 Schematic Editing — Implementation Plan (Plan 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** Native, *surgical* edits to a KiCad-10 `.kicad_sch` — change a component's Value / Footprint association / any instance property in place, behind a copy→ERC→diff→approve→apply guard, with **zero risk of dropping KiCad-10 tokens** (no full-file resave).

**Architecture:** A new `kicad_mcp/edit/` package. `locate` finds a placed symbol instance by Reference (parsing with the existing `parse.py` sexpdata helpers) and returns its **UUID** + current property values. `surgical` does a **UUID-anchored span replacement**: anchor on the instance's globally-unique `(uuid "…")`, find the next `(property "<name>" "<old>"`, and replace only the quoted value — leaving the rest of the file byte-identical. `guard` runs the edit on a temp copy, re-runs `kicad-cli sch erc`, and returns a unified diff + ERC delta; it writes the live file only on approval and only if ERC didn't regress.

**Tech Stack:** Python 3.10+, `sexpdata` (semantic locate), stdlib `re`/`difflib`/`shutil`, the existing `kicad.run_erc`. Plan 3 (part-sourcing + place-symbol) is separate.

**Why this is safe (vs the kicad-sch-api risk):** we never deserialize-and-resave the whole file through a foreign writer. The edit is a byte-span replacement of one quoted string. `sexpdata` is used read-only to *locate*; the write is textual. KiCad-10-only constructs elsewhere in the file are untouched.

---

### Task 1: `locate.py` — find a placed instance by Reference

**Files:** Create `kicad_mcp/edit/__init__.py`, `kicad_mcp/edit/locate.py`; Test `tests/test_edit_locate.py`.

- [ ] Write a golden test: parse the real PERIPH copy, `find_instance("C9")` returns an `Instance` with `reference=="C9"`, a 36-char `uuid`, `value` (e.g. "4.7uF"), and a `Footprint` string; `find_instance("NOPE")` returns `None`. Distinguish placed instances from the `lib_symbols` cache (only nodes with a `lib_id` AND an `instances` block count).
- [ ] Implement `find_instance(sch_path, reference) -> Instance | None` reusing `parse._head/_get/_getval/_getall/_sym`. Iterate `data[1:]`, select `(symbol …)` nodes that have a `lib_id` child, match the `(property "Reference" …)`; return `Instance(reference, uuid, value, footprint, lib_id)`.
- [ ] Run → PASS. Commit.

### Task 2: `surgical.py` — UUID-anchored property edit

**Files:** Create `kicad_mcp/edit/surgical.py`; Test `tests/test_edit_surgical.py`.

- [ ] Golden test on a PERIPH **copy**: `set_property(copy_sch, "C9", "Value", "10uF")` →
  (a) `find_instance` now reports value "10uF"; (b) **every other byte of the file is unchanged** except that one quoted value (assert via difflib that exactly one hunk changed and it's the C9 value); (c) the file still parses with `sexpdata` and re-opens (`kicad-cli sch erc` runs without a load error).
- [ ] Implement `set_property(sch_path, reference, prop_name, new_value)`: locate → read text → `ai = text.index(f'(uuid "{inst.uuid}")')` → regex `\(property "<name>" "((?:[^"\\]|\\.)*)"` searched from `ai` → replace group(1) with the escaped `new_value` → write. Raise `EditError` if the instance or property isn't found. Add thin `set_value` / `set_footprint` wrappers.
- [ ] Run → PASS. Commit.

### Task 3: `guard.py` — copy → ERC → diff → approve → apply

**Files:** Create `kicad_mcp/edit/guard.py`; Test `tests/test_edit_guard.py`.

- [ ] Golden test: `propose_edit(project, "C9", "Value", "10uF")` returns a dict with `diff` (unified, one hunk), `erc_before`/`erc_after` violation counts (no new errors), and `applied=False` — and the **live** sch is unchanged. Then `apply=True` writes it and the live value is "10uF".
- [ ] Implement: copy the project dir to a tempdir (`shutil.copytree`), run `set_property` on the copy, `kicad.run_erc` on the copy (compare error counts to a baseline ERC of the original), build a `difflib.unified_diff`. If `apply` and ERC errors didn't increase, write the new text to the real sch (atomic: write temp + `os.replace`). Never touch the live file unless `apply=True`.
- [ ] Run → PASS. Commit.

### Task 4: surfaces — CLI + MCP + skill

**Files:** Modify `lib/kicad_review_cli.py`, `kicad_mcp/tools/review_tools.py`, `skills/kicad-design/SKILL.md`; Test extends `tests/test_edit_guard.py`.

- [ ] CLI: `set-value <project> <ref> <value> [--apply]` and `set-footprint <project> <ref> <lib:fp> [--apply]` → call `guard.propose_edit`, print the diff + ERC delta; without `--apply` it's a dry run.
- [ ] MCP: `kicad_set_value(project, reference, value, apply=False)` / `kicad_set_footprint(...)` wrapping `guard.propose_edit` (behind `_safe`).
- [ ] SKILL.md: add an "Editing (v1)" section — always dry-run first, show the diff + ERC delta, get the human's OK, then `--apply`; never edit the live file blindly.
- [ ] Run the full suite + ruff; commit; push; watch CI.

---

## Self-Review
- **Spec coverage:** surgical Value/Footprint/property edits (§5 `locate`/`surgical`/`guard`), the copy→diff→ERC→approve→apply guard (§4), CLI/MCP/skill surfaces (§5). Place-symbol + part-sourcing are Plan 3 (out of scope). ✓
- **Placeholder scan:** each task has a concrete golden test + implementation approach. ✓
- **Risk:** no full-file resave → no KiCad-10 token-drop. The verification gate the spec calls out for *full-file* writers (Plan 3 place-symbol) is not needed for these span edits — but the golden tests still confirm re-parse + ERC after every edit. ✓
