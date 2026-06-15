---
description: Quick structured summary of a KiCad project — files, net classes, copper layers, track widths, counts.
argument-hint: <project dir or .kicad_pro/.kicad_sch/.kicad_pcb>
---

# Inspect a KiCad Project

Fast, read-only orientation before any deeper review. Use this to learn what a project contains without opening KiCad.

## Command

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py inspect $ARGUMENTS
```

## After running

Summarize cleanly:
- **Files** — schematic / pcb / project present.
- **Net classes** — names + track widths. Flag if there is only a single `Default` class.
- **Board** — copper layer count, copper weight, track/via/footprint counts, and the track-width distribution. Flag a power board sitting at very thin minimum widths.

Do not draw the board. For a visual, use `/kicad:render` then `Read` the images, or run `/kicad:review` for the full pass.
