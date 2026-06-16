# kicad-review Bug-Fix Batch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 9 confirmed bugs (11 `xfail` tests) in the review engine so the comprehensive suite goes from "green-with-xfails" to genuinely green — the prerequisite for v1 schematic editing (the BLOCKER stale-read fix is required by the edit guard).

**Architecture:** Pure TDD against the existing suite. Every fix already has a `@pytest.mark.xfail(strict=True)` test encoding the *correct* behavior. Each task: (1) delete the `xfail` marker, (2) run → confirm it now FAILS red (proving the bug), (3) apply the fix, (4) run → PASS + run the full review suite for no regressions, (5) commit. `strict=True` means a fix that works flips the test from xfail→pass cleanly.

**Tech Stack:** Python 3.10+, pytest, ruff 0.12.4, `uv` (`.venv` in the repo). Plugin root: `C:\Users\jonny\.claude\plugins\local\kicad-review`.

**This is Plan 1 of 3 for v1:** (1) **bug-fix batch ← this plan**; (2) schematic editing engine (surgical value/footprint edits + copy→diff→ERC→approve guard + KiCad-10 verification gate); (3) part-sourcing chain (local-libs → easyeda2kicad pull → AI-draft) + gated place-symbol.

---

## Conventions for every task

- **Run command** (from the plugin dir, real board present): `set KICAD_CLI_PATH=C:\Program Files\KiCad\10.0\bin\kicad-cli.exe && set PYTHONUTF8=1 && py -m uv run python -m pytest <target> -o addopts="" -v`
- **Full-suite regression check:** `py -m uv run python -m pytest tests/test_review_*.py -o addopts="" -q`
- **Ruff (after any code edit):** `py -m uv run ruff check <file>` then `py -m uv run ruff format <file>`
- **Commit identity** is already repo-local (`jonnyliu4@gmail.com` / `JonathanLiu1401`). End every commit message with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Do NOT push** until all tasks are done and the full suite is green; push is the final step (Task 10).

## File map (what each task touches)

| Task | Bug | Fix file | Test (remove xfail) |
|---|---|---|---|
| 1 | BLOCKER stale-artifact read | `kicad_mcp/review/kicad.py` | `tests/test_review_kicad.py::test_run_erc_raises_on_cli_failure_does_not_read_stale` |
| 2 | datasheet-discovery crash | `kicad_mcp/review/engine.py` | `tests/test_review_engine_surfaces.py::test_datasheet_discovery_crash_does_not_discard_review` |
| 3 | MCP `_safe` too narrow | `kicad_mcp/tools/review_tools.py` | `tests/test_review_engine_surfaces.py::test_mcp_safe_catches_non_kicad_errors` |
| 4 | excluded ERC/DRC re-reported | `kicad_mcp/review/checks.py` | `tests/test_review_checks.py::test_check_erc_excluded_violation_not_reported`, `::test_check_drc_excluded_violation_not_reported` |
| 5 | decoupling `C`-prefix | `kicad_mcp/review/checks.py` | `tests/test_review_checks.py::test_decoupling_connector_or_crystal_not_a_cap` |
| 6 | decoupling per-net not per-pin | `kicad_mcp/review/checks.py` | `tests/test_review_checks.py::test_decoupling_shared_rail_far_ics_flagged` |
| 7 | IPC `external=True` inner layer | `kicad_mcp/review/{parse,checks}.py` | `tests/test_review_checks.py::test_inner_layer_undersized_flagged` |
| 8 | `to_markdown` INFO header | `kicad_mcp/review/report.py` | `tests/test_review_report.py::test_markdown_header_counts_sum_to_total_includes_info` |
| 9 | ambiguous `discover_project` | `kicad_mcp/review/kicad.py` | `tests/test_review_kicad.py::test_discover_project_ambiguous_two_projects` |

---

### Task 1: BLOCKER — stale-artifact read (kicad.py runners)

**Files:** Modify `kicad_mcp/review/kicad.py`; Test `tests/test_review_kicad.py`.

- [ ] **Step 1:** In `tests/test_review_kicad.py`, delete the `@pytest.mark.xfail(reason="BLOCKER: stale-artifact read ...", strict=True)` decorator above `test_run_erc_raises_on_cli_failure_does_not_read_stale` (keep `@requires_cli`).

- [ ] **Step 2: Run → confirm FAIL.** `pytest tests/test_review_kicad.py::test_run_erc_raises_on_cli_failure_does_not_read_stale -v` → FAIL ("DID NOT RAISE KiCadError"): run_erc returns the stale dict.

- [ ] **Step 3: Add a `_run_to_file` helper and route every runner through it.** In `kicad.py`, after `_run(...)`:

```python
def _run_to_file(args: list[str], dest: Path, timeout: int, what: str):
    """Produce `dest` via kicad-cli. Clear any stale file first, and fail loudly on a
    nonzero exit so a previous run's artifact is never returned silently."""
    dest = Path(dest)
    dest.unlink(missing_ok=True)
    r = _run(args, timeout=timeout)
    if r.returncode != 0 or not dest.is_file():
        raise KiCadError(f"{what} failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()}")
    return r
```

Then replace the body of each runner's `_run(...)`/`if not dest.is_file()` block with a `_run_to_file` call. Example for `run_erc`:

```python
def run_erc(project: Project, out: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    if not project.sch:
        raise KiCadError("No schematic to run ERC on.")
    cli = find_kicad_cli()
    dest = workdir(project, out) / "erc.json"
    _run_to_file(
        [cli, "sch", "erc", "--format", "json", "--severity-all",
         "--output", str(dest), str(project.sch)],
        dest, timeout, "ERC",
    )
    return json.loads(dest.read_text(encoding="utf-8"))
```

Apply the identical pattern (clear dest → run → check returncode) to: `run_drc` (`"DRC"`), `export_netlist` (`"netlist export"`), `export_bom` (`"BOM export"`), `render_schematic_pdf` (`"schematic render"`), `render_board_pdf` (`"board render"`), `render_3d` (`"3D render"`) — each passing its own args + dest + label.

- [ ] **Step 4: Run → PASS + regression.** The targeted test PASSES; then run the full suite (`tests/test_review_*.py`) — all green (this also re-exercises run_erc/run_drc/renders on the real board via the integration tests).

- [ ] **Step 5: Ruff + commit.** `ruff check`/`format` on kicad.py; `git commit -m "fix: runners must not return a prior run's artifact on kicad-cli failure"` (+ co-author trailer).

---

### Task 2: datasheet-discovery crash discards the finished review (engine.py)

**Files:** Modify `kicad_mcp/review/engine.py`; Test `tests/test_review_engine_surfaces.py`.

- [ ] **Step 1:** Remove the `xfail` decorator above `test_datasheet_discovery_crash_does_not_discard_review` (keep `@requires_board`).
- [ ] **Step 2: Run → FAIL** (review() raises OSError because `_find_datasheets()` is called unguarded at the return).
- [ ] **Step 3: Wrap it in `_stage` and compute it before assembling findings.** In `review()`, just before `findings = sort_findings(self._findings)`, add:

```python
datasheets = self._stage("datasheet discovery", self._find_datasheets) or []
```

and change the return dict entry from `"datasheets": self._find_datasheets(),` to `"datasheets": datasheets,`. (Computing it before `findings` means a failure surfaces as a stage-fail INFO finding instead of crashing.)

- [ ] **Step 4: Run → PASS + full-suite regression** (green).
- [ ] **Step 5: Ruff + commit** `"fix: datasheet discovery failure must not discard a completed review"`.

---

### Task 3: MCP `_safe` catches only KiCadError (review_tools.py)

**Files:** Modify `kicad_mcp/tools/review_tools.py`; Test `tests/test_review_engine_surfaces.py`.

- [ ] **Step 1:** Remove the `xfail` decorator above `test_mcp_safe_catches_non_kicad_errors`.
- [ ] **Step 2: Run → FAIL** (TimeoutExpired escapes `_safe`).
- [ ] **Step 3: Broaden the except.** In `_safe`:

```python
def _safe(fn):
    """Return a structured ``{"error": ...}`` for ANY failure, matching the CLI's
    clean-error contract (not just KiCadError)."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001 - deliberate: every failure -> structured error
            return {"error": f"{type(e).__name__}: {e}"}
    return wrapper
```

If `from kicad_mcp.review.kicad import KiCadError` is now unused in the file, remove that import (ruff F401 will flag it).

- [ ] **Step 4: Run → PASS + full-suite regression.**
- [ ] **Step 5: Ruff + commit** `"fix: MCP _safe returns a structured error for all exceptions"`.

---

### Task 4: excluded (user-suppressed) ERC/DRC violations re-reported (checks.py)

**Files:** Modify `kicad_mcp/review/checks.py`; Test `tests/test_review_checks.py`.

- [ ] **Step 1:** Remove the `xfail` decorators above `test_check_erc_excluded_violation_not_reported` and `test_check_drc_excluded_violation_not_reported`.
- [ ] **Step 2: Run both → FAIL** (excluded items come back as MAJOR/MINOR).
- [ ] **Step 3: Filter `excluded` before triage.** In `_erc_violations`, change the return to drop excluded:

```python
def _erc_violations(erc: dict) -> list[dict]:
    out = []
    for s in erc.get("sheets", []) or []:
        out += s.get("violations", []) or []
    out += erc.get("violations", []) or []
    return [v for v in out if not v.get("excluded")]
```

In `check_drc`, change the first line that reads violations to:

```python
viol = [v for v in (drc.get("violations", []) or []) if not v.get("excluded")]
```

- [ ] **Step 4: Run both → PASS + full-suite regression.**
- [ ] **Step 5: Ruff + commit** `"fix: do not re-report user-excluded ERC/DRC violations"`.

---

### Task 5: decoupling counts connectors/crystals as caps (checks.py)

**Files:** Modify `kicad_mcp/review/checks.py`; Test `tests/test_review_checks.py`.

- [ ] **Step 1:** Remove the `xfail` decorator above `test_decoupling_connector_or_crystal_not_a_cap`.
- [ ] **Step 2: Run → FAIL** (`CONN1`/`CR1` suppress the missing-decap finding).
- [ ] **Step 3: Require a real cap refdes (`C` + digit).** In `check_decoupling`, replace the `caps_on_net` comprehension:

```python
caps_on_net = [nd["ref"] for nd in net_nodes.get(net, []) if re.fullmatch(r"C\d.*", nd["ref"])]
```

(`re` is already imported in checks.py. `C\d.*` matches `C1`, `C12` but not `CONN1`/`CR1`.)

- [ ] **Step 4: Run → PASS + full-suite regression.**
- [ ] **Step 5: Ruff + commit** `"fix: decoupling check ignores connectors/crystals (C\\d refdes only)"`.

---

### Task 6: decoupling judged per-net, not per-IC-pin (checks.py)

**Files:** Modify `kicad_mcp/review/checks.py`; Test `tests/test_review_checks.py`.

- [ ] **Step 1:** Read `tests/test_review_checks.py::test_decoupling_shared_rail_far_ics_flagged` to get the exact synthetic positions, distance threshold, and expected severity it asserts. Then remove its `xfail` decorator.
- [ ] **Step 2: Run → FAIL** (one cap on a shared rail suppresses `decap-missing` for the far ICs).
- [ ] **Step 3: Make the check per-IC.** In `check_decoupling`, for each `(ic, net)`: if `caps_on_net` is empty → existing MAJOR "no bypass capacitor". Otherwise, if a board is available, compute the nearest cap distance **to this IC** and flag this IC when its nearest cap exceeds the local-decoupling threshold (a cap elsewhere on the rail no longer satisfies it). Reference implementation (tune the constant/severity to match the test from Step 1):

```python
_DECAP_LOCAL_MM = 10.0  # a bypass cap beyond this is not "local" decoupling for the pin

# inside the `elif board and ic in pos:` branch, replacing the current >5.0 block:
dists = [math.hypot(pos[ic][0] - pos[c][0], pos[ic][1] - pos[c][1])
         for c in caps_on_net if c in pos]
if dists and min(dists) > _DECAP_LOCAL_MM:
    findings.append(Finding(
        id=f"decap-far-{ic}-{net}", severity=Severity.MAJOR, domain=Domain.POWER_THERMAL,
        title=f"{ic}: no local bypass cap on '{net}' (nearest is {min(dists):.1f} mm)",
        detail=f"The nearest cap on '{net}' is {min(dists):.1f} mm from {ic} — too far to "
               "decouple this IC. Each IC power pin needs its own adjacent bypass cap.",
        recommendation=f"Add a 100 nF cap on '{net}' within a couple mm of {ic}'s power pin.",
        location={"refdes": ic, "net": net}, evidence="netlist + board coords",
        check="decoupling",
    ))
```

- [ ] **Step 4: Run → PASS + full-suite regression** (re-check the PERIPH integration assertions in `test_review_periph.py`/`test_review_checks.py` still hold; adjust the threshold if a real-board finding count assertion shifts).
- [ ] **Step 5: Ruff + commit** `"fix: decoupling proximity is per-IC-pin, not per-net"`.

---

### Task 7: IPC-2221 hardcodes outer-layer k for inner-layer tracks (checks.py + parse.py)

**Files:** Modify `kicad_mcp/review/checks.py` and (if needed) `kicad_mcp/review/parse.py`; Test `tests/test_review_checks.py`.

- [ ] **Step 1:** Read `test_inner_layer_undersized_flagged` (and the helper `test_inner_layer_xfail_numbers_are_in_the_discriminating_band`) for the exact Board/Track inputs and the current band. Remove the `xfail` decorator.
- [ ] **Step 2: Run → FAIL** (an inner-layer track is judged with k=0.048 and not flagged).
- [ ] **Step 3: Plumb the min-width segment's layer into the capacity calc.** In `check_trace_currents`, track the *layer* of the thinnest sustained segment per net, then choose k by layer. Replace the per-net min-width accumulation so it remembers the layer, and set `external` from it:

```python
# per net: (min sustained width, layer of that segment)
min_w: dict[int, tuple[float, str]] = {}
for nid, segs in segs_by_net.items():
    sustained = [(t.width, t.layer) for t in segs if t.length >= _MIN_SUSTAINED_LEN]
    pool = sustained or [(t.width, t.layer) for t in segs]
    min_w[nid] = min(pool, key=lambda wl: wl[0])
...
for nid, (w, layer) in sorted(min_w.items(), key=lambda kv: kv[1][0]):
    ...
    external = not str(layer).startswith("In")  # In1.Cu / In2.Cu are inner layers
    cap = ipc2221_capacity_a(w, dT_c=dT_c, copper_oz=board.copper_oz, external=external)
    # ... pass `external` to the ipc2221_width_mm(...) calls in this loop too
```

(`Track` already carries `.layer`. Update both `ipc2221_capacity_a` and `ipc2221_width_mm` calls in the loop to use `external`.)

- [ ] **Step 4: Run → PASS + full-suite regression** (PERIPH has no inner-layer tracks, so its findings are unchanged — confirm `test_review_periph.py` still green).
- [ ] **Step 5: Ruff + commit** `"fix: IPC-2221 uses inner-layer k (0.024) for In*.Cu tracks"`.

---

### Task 8: `to_markdown` summary header omits INFO (report.py)

**Files:** Modify `kicad_mcp/review/report.py`; Test `tests/test_review_report.py`.

- [ ] **Step 1:** Remove the `xfail` decorator above `test_markdown_header_counts_sum_to_total_includes_info`.
- [ ] **Step 2: Run → FAIL** (header shows e.g. "1 blocker (total 3)" — sums don't match).
- [ ] **Step 3: Include `info` in the header loop.** In `to_markdown`, the severity tuple in the summary line:

```python
        + ", ".join(
            f"{_SEV_ICON.get(s,'')} {sev.get(s,0)} {s}"
            for s in ("blocker", "major", "minor", "nit", "info")
            if sev.get(s)
        )
```

- [ ] **Step 4: Run → PASS + full-suite regression.**
- [ ] **Step 5: Ruff + commit** `"fix: markdown summary header counts INFO findings"`.

---

### Task 9: `discover_project` silently resolves ambiguous dirs (kicad.py)

**Files:** Modify `kicad_mcp/review/kicad.py`; Test `tests/test_review_kicad.py`.

- [ ] **Step 1:** Read `test_discover_project_ambiguous_two_projects` for the exact expected behavior (it is `strict=False`). Remove its `xfail` decorator.
- [ ] **Step 2: Run → FAIL** (it picks `pros[0]` silently).
- [ ] **Step 3: Raise on ambiguity.** In `discover_project`, after computing `pros = _real(sorted(directory.glob("*.kicad_pro")))`:

```python
        if len(pros) > 1:
            raise KiCadError(
                "Ambiguous project directory: multiple .kicad_pro files "
                f"({', '.join(p.name for p in pros)}). Pass a specific file."
            )
        if pros:
            stem = pros[0].stem
```

- [ ] **Step 4: Run → PASS + full-suite regression** (single-project discovery still works).
- [ ] **Step 5: Ruff + commit** `"fix: discover_project raises on ambiguous multi-project dirs"`.

---

### Task 10: Final verification + push

- [ ] **Step 1: Whole suite, no xfails left for these bugs.** `pytest tests/test_review_*.py -o addopts="" -q -rxX` → all pass; the only remaining xfails (if any) are unrelated. Expect **0 xpass-strict failures**.
- [ ] **Step 2: Reproduce CI exactly (no board).** `set KICAD_REVIEW_TEST_PROJECT=/nonexistent && py -m uv run python -m pytest tests/test_review_*.py -o addopts="" --cov=kicad_mcp/review --cov-fail-under=70 -q` → PASS, coverage ≥ 70%. Also `ruff check` + `ruff format --check` on the review-layer paths.
- [ ] **Step 3: Push** to `origin main` (ephemeral-token method, active account `JonathanLiu1401`), then watch CI green via `gh run watch`.
- [ ] **Step 4:** Update `MEMORY.md` project note: "11 xfail bugs FIXED (commit …); suite genuinely green."

---

## Self-Review

- **Spec coverage:** all 9 confirmed bugs (11 xfail tests) from the work-checker pass have a task; the BLOCKER (Task 1) is first because the v1 edit guard depends on `run_erc` returning fresh results. ✓
- **Placeholder scan:** Tasks 6 and 7 reference reading the exact test for thresholds/band before tuning the constant — intentional (the strict-xfail test is the precise acceptance spec); fix code is provided, only the numeric constant/severity is confirmed against the test. All other tasks have complete code. ✓
- **Type consistency:** `_run_to_file(args, dest, timeout, what)` used uniformly (Task 1); `Finding(...)` fields match `report.py`; `Track.layer` exists (parse.py). ✓
- **Scope:** single subsystem (review-engine bug fixes), self-contained, ships a genuinely-green suite. v1 editing + part-sourcing are Plans 2–3. ✓
