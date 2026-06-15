---
description: Render a KiCad schematic + board + 3D to images, then read them.
argument-hint: <project> [--what all|sch|board|3d]
---

# Render a KiCad Design

Produce images you can actually look at.

## Command

```
py C:\Users\jonny\.claude\plugins\local\kicad-review\lib\kicad_review_cli.py render $ARGUMENTS
```

This prints the path of each rendered file: schematic PDF, board front/back/copper PDFs, and a 3D PNG.

## After running

**`Read` every path it printed.** That is the whole point — the tool generates the images; you are the one who can see them. Then describe what you observe: placement, routing density, copper pours, return paths, thin necks, silkscreen, connector/edge layout.

Do not guess at the layout from numbers — read the images.
