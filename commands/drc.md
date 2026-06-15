---
description: Run KiCad DRC (design rules check) + schematic↔board parity headlessly and summarize.
argument-hint: <project dir or .kicad_pcb/.kicad_pro>
---

# Run DRC

Headless design rules check (with schematic↔board parity) via kicad-cli.

## Command

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py drc $ARGUMENTS
```

## After running

Summarize: violation count, unconnected items, and **schematic-parity issues**. A nonzero parity count is a **major** finding — the PCB does not match the schematic (missing footprints, net conflicts); recommend "Update PCB from Schematic" (F8) before fabrication. Group violations by type and call out anything safety/manufacturing-critical (clearance, shorts, solder-mask bridges). For the full picture run `/kicad:review`.
