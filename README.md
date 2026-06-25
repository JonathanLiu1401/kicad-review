# eda-review

AI-assisted EDA review for PCB teams that need more than a chat bot looking at screenshots.

`eda-review` gives an agent structured access to the schematic, PCB layout, BOM, design rules,
stackup, rendered board views, and manufacturing outputs. It started as a fork of
[`lamaalrajih/kicad-mcp`](https://github.com/lamaalrajih/kicad-mcp), then grew into a
review-first toolchain for KiCad and Altium boards.

The goal is practical: catch electrical, layout, BOM, and fabrication problems before an order
goes to the manufacturer.

## What eda-review can do

- **Understand schematics and layouts together.** Inspect projects, export netlists, compare
  schematic-to-PCB parity, run ERC/DRC, and render schematic, board, copper, and 3D views for
  visual review.
- **Manage BOM risk.** Extract MPNs from designs, group references, flag missing or placeholder
  part data, check JLCPCB/LCSC and DigiKey availability, and search JLCPCB alternatives.
- **Review for manufacturing.** Check board geometry, configured rules, stackup, via limits, and
  copper constraints against JLCPCB-focused capabilities. Run fab readiness checks before handoff.
- **Generate handoff files.** Export Gerbers, drill files, pick-and-place/CPL data, and STEP from
  KiCad through official KiCad export paths.
- **Support KiCad and Altium workflows.** KiCad support is direct through `kicad-cli` and project
  files. Altium support uses the `eda-agent` bridge to turn live Altium data into the same shared
  review pipeline.
- **Keep edits guarded.** Deterministic edits use dry-run diffs first, require explicit `--apply`,
  and re-check loadability or rules where the backend supports it.

## Why this exists

LLMs are useful reviewers when they have evidence. They are risky when they guess.

`eda-review` puts deterministic tooling in front of the agent:

- KiCad CLI output for ERC, DRC, rendering, netlists, BOMs, and fab exports.
- Parsed schematic and PCB facts for rule checks and layout reasoning.
- Distributor data for live sourcing and stock validation.
- EDA-neutral manufacturing facts that both KiCad and Altium backends can feed.
- Rendered images the agent can actually inspect before commenting on placement, routing, copper
  pours, silkscreen, assembly orientation, or 3D seating.

The result is a review workflow that can talk about the board from both sides: what the schematic
intends and what the physical layout will send to a fab.

## Backend support

| Backend     | Status              | What it covers                                                                                                                         |
| ----------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| KiCad       | Primary path        | `.kicad_pro`, `.kicad_sch`, `.kicad_pcb`, ERC/DRC, netlist, rendered review, BOM checks, JLCPCB DFM checks, guarded edits, fab exports |
| Altium      | Experimental bridge | Live Altium data through `eda-agent`, BOM sourcing, rule/stackup/geometry normalization, JLCPCB manufacturability grading              |
| Shared core | Stable foundation   | Distributor stock checks, BOM sourcing sweeps, JLCPCB capability grading, EDA-neutral board facts                                      |

Altium support depends on a running Altium session and the third-party
[`eda-agent`](https://github.com/salitronic/eda-agent) MCP bridge. Before trusting an Altium review,
run the setup spike in [docs/altium_eda_agent_setup.md](docs/altium_eda_agent_setup.md) so the
bridge output is confirmed against your Altium version.

## Quick start

Install KiCad 9 or newer, then install the Python dependencies:

```powershell
git clone https://github.com/JonathanLiu1401/eda-review.git
cd eda-review
py -m pip install -r requirements.txt
```

KiCad ships `kicad-cli`. If it is not on your `PATH`, set `KICAD_CLI_PATH` to the full executable
path.

Run a review:

```powershell
py lib\kicad_review_cli.py inspect C:\path\to\board\board.kicad_pro
py lib\kicad_review_cli.py review  C:\path\to\board\board.kicad_pro --scope all
py lib\kicad_review_cli.py erc     C:\path\to\board\board.kicad_pro
py lib\kicad_review_cli.py drc     C:\path\to\board\board.kicad_pro
py lib\kicad_review_cli.py render  C:\path\to\board\board.kicad_pro --what all
```

Run BOM and manufacturing checks:

```powershell
py lib\kicad_review_cli.py check-bom     C:\path\to\board\board.kicad_pro
py lib\kicad_review_cli.py check-stock   RC0603FR-0710KL
py lib\kicad_review_cli.py search-parts  "10k 0603 resistor"
py lib\kicad_review_cli.py jlcpcb-check  C:\path\to\board\board.kicad_pro
py lib\kicad_review_cli.py fab-check     C:\path\to\board\board.kicad_pro
py lib\kicad_review_cli.py fab-export    C:\path\to\board\board.kicad_pro --out C:\path\to\fab
```

`review` writes an evidence package under the board's `.kicad-review/` directory, including a
Markdown report, JSON facts, and render paths for the agent to inspect.

## Main CLI commands

| Command                | Purpose                                                                                |
| ---------------------- | -------------------------------------------------------------------------------------- |
| `inspect`              | Summarize project files, board layers, track widths, vias, footprints, and net classes |
| `review`               | Run the full evidence-building review workflow                                         |
| `erc` / `drc`          | Run KiCad electrical and design rule checks and summarize violations                   |
| `render`               | Export schematic, board, copper, and 3D views                                          |
| `netlist`              | Export a KiCad netlist                                                                 |
| `check-bom`            | Check every MPN in the schematic BOM against JLCPCB/LCSC and DigiKey                   |
| `check-stock`          | Check one part number or LCSC code for live availability                               |
| `search-parts`         | Search JLCPCB/LCSC for stock-ranked candidate parts                                    |
| `jlcpcb-check`         | Compare the board against JLCPCB-focused capability and stackup checks                 |
| `jlcpcb-apply-rules`   | Dry-run or apply JLCPCB minimum design rules to a KiCad project                        |
| `jlcpcb-apply-stackup` | Dry-run or apply a JLCPCB reference stackup to a KiCad PCB                             |
| `fab-check`            | Check fabrication readiness and produce a fab package for inspection                   |
| `fab-export`           | Export Gerbers, drill, pick-and-place, and STEP files                                  |
| `set-property`         | Dry-run or apply a targeted component property change, such as an MPN update           |

## Use as an MCP server

The CLI is the most reliable path for automated review, but the repo still includes the upstream
MCP server interface. Register it with an MCP-capable client:

```powershell
claude mcp add kicad -- py C:\path\to\eda-review\main.py
```

This exposes KiCad project, schematic, PCB, DRC, BOM, rendering, and review tools through MCP.

## Repository layout

```text
eda-review/
  eda_core/       Shared BOM, stock, JLCPCB, and board-fact logic
  kicad_mcp/      KiCad backend, MCP server, parsing, review, edit, and fab code
  altium_review/  Altium adapter and shared review entry points
  lib/            CLI entry point used by agents and CI
  skills/         Agent workflow instructions for KiCad and Altium design review
  commands/       Claude Code command wrappers
  docs/           Setup notes, design notes, and Altium bridge guidance
  tests/          Unit and integration coverage for review, BOM, DFM, fab, and edits
```

## Development

Install the test dependency, then run the suite:

```powershell
py -m pip install pytest
py -m pytest -q
```

Some integration tests need KiCad and a real project path. The unit tests cover the shared
manufacturing logic, distributor normalization, Altium adapter, report model, and guarded edits
without requiring KiCad or Altium.

## Safety model

`eda-review` is intentionally conservative:

- Use official EDA export/check functions when they exist.
- Treat KiCad and Altium project files as the ground truth.
- Prefer dry-run diffs before edits.
- Keep routing and placement as review guidance unless a deterministic backend operation is
  explicitly requested.
- Verify generated fab files before treating them as manufacturing-ready.

## License

MIT. This project builds on the MIT-licensed KiCad MCP work by
[`lamaalrajih/kicad-mcp`](https://github.com/lamaalrajih/kicad-mcp).
