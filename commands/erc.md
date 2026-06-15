---
description: Run KiCad ERC (electrical rules check) headlessly and summarize.
argument-hint: <project dir or .kicad_sch/.kicad_pro>
---

# Run ERC

Headless electrical rules check via kicad-cli.

## Command

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py erc $ARGUMENTS
```

## After running

Summarize: total violations, the error vs warning split, and the top violation types. Call out **disabled checks** — a disabled ERC check hides real problems, so confirm each was disabled deliberately. Treat `power_pin_not_driven` (often a missing PWR_FLAG) and `pin_to_pin` (e.g. two outputs shorted) as worth a closer look. For the full picture run `/kicad:review`.
