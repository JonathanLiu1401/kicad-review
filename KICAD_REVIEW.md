# kicad-review — design-review layer

This repo is a fork of [`lamaalrajih/kicad-mcp`](https://github.com/lamaalrajih/kicad-mcp)
(MIT) repackaged as a **Claude Code local plugin** and extended with an
engineering **design-review engine**. It lets Claude read, understand, and review
KiCad 9/10 schematics and PCBs — both schematic and layout.

**v0 is read-only.** It suggests improvements; you apply them in KiCad. (Guarded,
human-approved edits are the planned v1.)

## What was added on top of the fork

```
kicad_mcp/review/            # the review engine (pure Python; sexpdata + kicad-cli)
  kicad.py                   #   kicad-cli location, project discovery, headless runners
  parse.py                   #   .kicad_pcb / .kicad_pro / netlist parsers
  checks.py                  #   deterministic EE checks (IPC-2221, decoupling, net class, ...)
  report.py                  #   Finding model + markdown/JSON report
  engine.py                  #   orchestration -> evidence package
kicad_mcp/tools/review_tools.py   # FastMCP wrappers: kicad_review / inspect / erc / drc / render
lib/kicad_review_cli.py      # Bash/CI CLI shim — the PRIMARY path the skill uses
skills/kicad-design/SKILL.md # the Claude Code workflow primer
commands/                    # /kicad:review, :inspect, :render, :erc, :drc
tests/test_review_periph.py  # golden tests (IPC-2221 math + real-board findings)
```

## What it checks

- **ERC / DRC** — headless via `kicad-cli`, triaged by type/severity.
- **Schematic↔board parity** — flags a PCB out of sync with its schematic.
- **ERC suppression audit** — surfaces disabled/downgraded checks that hide problems.
- **Net-class audit** — flags single-`Default`-class boards.
- **Power/thermal** — IPC-2221 trace-current capacity per power net; pass/fail with `--current`.
- **Decoupling** — IC power-input nets missing a bypass cap; cap→pin distance from board coords.
- **BOM hygiene** — missing/placeholder values.
- **Render-and-Read** — schematic PDF, board front/back/copper PDFs, and a 3D PNG that Claude
  reads to judge placement, routing, pours, and return paths, cross-checked against datasheets.

The deterministic checks produce exact facts; the Claude skill adds the visual + datasheet
judgment (the part only an LLM looking at the images can do).

## Install

```powershell
# core (CLI + skill path — all you need for Claude Code)
py -m pip install --user sexpdata
# optional: to run the MCP server
py -m pip install --user fastmcp "mcp[cli]"
# optional: tests
py -m pip install --user pytest
```

KiCad 9 or 10 must be installed (`kicad-cli` ships with it). The engine auto-detects the
newest `kicad-cli`; override with `KICAD_CLI_PATH`.

## Use it (CLI / skill path — primary)

The `kicad-design` skill auto-loads in Claude Code and drives the CLI. Manually:

```powershell
py lib\kicad_review_cli.py inspect  <project>
py lib\kicad_review_cli.py review   <project> [--scope all|schematic|layout] [--current "12V=4.0"]
py lib\kicad_review_cli.py erc       <project>
py lib\kicad_review_cli.py drc       <project>
py lib\kicad_review_cli.py render    <project> [--what all|sch|board|3d]
```

`<project>` is a directory or any `.kicad_pro` / `.kicad_sch` / `.kicad_pcb`. `review`
writes `review-<name>.md` + `.json` to `<project>/.kicad-review/` and prints the image
paths for Claude to read.

## Use it (MCP server — optional)

Register the forked server so the `kicad_*` tools appear directly:

```powershell
claude mcp add kicad -- py C:\Users\jonny\.claude\plugins\local\kicad-review\main.py
```

Tools: `kicad_review`, `kicad_inspect`, `kicad_erc`, `kicad_drc`, `kicad_render` (plus the
fork's original tools). Requires `fastmcp` installed.

## Test

```powershell
py -m pytest tests/test_review_periph.py -o addopts="" -v
```

The IPC-2221 + classification tests always run; the integration tests run against
`KICAD_REVIEW_TEST_PROJECT` (default: the PERIPH board) and skip if absent.

## Roadmap

- **v1:** guarded human-approved edits — propose → diff → approve → apply (PCB via the KiCad
  IPC API / `kicad-python`; schematic via S-expression libraries), with ERC/DRC re-run after
  each edit and a manufacturing-export hard-block.
