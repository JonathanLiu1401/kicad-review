---
description: Full KiCad design review — runs ERC/DRC, engineering checks, and renders the board, then synthesizes a prioritized review.
argument-hint: <project dir or .kicad_pro/.kicad_sch/.kicad_pcb> [--scope all|schematic|layout]
---

# Review a KiCad Design

Read and review a KiCad schematic + PCB. v0 is **read-only** — suggest changes; the human applies them.

## Pre-check

1. Resolve `$ARGUMENTS` to a project: a directory, or a `.kicad_pro` / `.kicad_sch` / `.kicad_pcb` file.
2. If nothing KiCad-shaped is there, stop and ask the user for the path.

## Step 1 — inspect

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py inspect $ARGUMENTS
```

Note the net classes, copper layers/weight, and track-width spread before going further.

## Step 2 — full review

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py review $ARGUMENTS
```

This prints the deterministic findings, then a list of rendered images and datasheet pointers.

## Step 3 — READ THE IMAGES (most important)

`Read` every image the review lists (schematic PDF, board front/back/copper PDFs, 3D PNG). The tool cannot see them; **you can**. Judge placement, routing, pours, return paths, thin necks, connector/edge use. Layout findings only become real once you can see *where* they are.

## Step 4 — check datasheets

For each major IC, open its datasheet pointer and compare the layout to the vendor's layout recommendations (thermal-pad vias, sense/Kelvin routing, cap placement, loop area).

## Step 5 — synthesize

Present ONE prioritized review: **blocker → major → minor → nit**, each with a specific location (refdes / net / board area) and a concrete fix. Cite the deterministic finding ids or what you can see in an image. Never invent a measurement.

For a hard power/thermal pass-fail, re-run with expected currents, e.g.:
`... review $ARGUMENTS --current "12V=4.0" --current "Motor 1 Out 1=3.5"`
