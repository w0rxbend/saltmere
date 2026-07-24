---
title: "Parametric parts you can regenerate: driving FreeCAD from Python"
date: 2026-07-24
track: cad-3dprint
summary: "The point of parametric CAD is that one number change reshapes the whole part. FreeCAD's Python console turns that idea into a script you can version, diff, and reuse."
reading_time: 4
tags: [freecad, parametric, python, 3d-printing, cad]
sources:
  - title: "FreeCAD scripting basics"
    url: "https://wiki.freecad.org/FreeCAD_Scripting_Basics"
  - title: "FreeCAD Spreadsheet workbench"
    url: "https://wiki.freecad.org/Spreadsheet_Workbench"
---

Draw a bracket by hand and you have *a* bracket. Define it parametrically and you have every bracket — change the wall thickness and the holes, fillets, and mounting tabs all move to match. FreeCAD is open-source and, crucially, scriptable: everything the GUI does is Python underneath, so you can author a part as code.

## The console is the whole toolbox

Open **View → Panels → Python console** and the entire API is live. A parametric standoff in a few lines:

```python
import FreeCAD as App, Part

doc = App.newDocument("standoff")
height, outer_d, hole_d = 12.0, 8.0, 3.2   # <-- the parameters

body = Part.makeCylinder(outer_d/2, height)
bore = Part.makeCylinder(hole_d/2, height)
part = body.cut(bore)                        # tube = cylinder minus bore

Part.show(part)
doc.recompute()
```

Change `hole_d` from `3.2` to `4.3` (M3 clearance → M4), re-run, and the part regenerates. Because it's a script, that change is a one-line diff in git — your CAD history becomes as legible as your code history.

## Spreadsheet-driven models: parameters without code

For parts you'll hand to someone else, FreeCAD's **Spreadsheet workbench** is the bridge. Put named cells (`aliases`) like `wall`, `bolt_d`, `count` in a sheet, then reference them in sketch constraints as `Spreadsheet.wall`. Now the model has a single table of inputs a non-programmer can edit, and the geometry recomputes from it. It's the same idea as the script above — one source of truth for the numbers — with a friendlier front door.

## The part that matters for printing

Parametric intent should include *print* parameters, not just geometry. Bake your printer's realities into the numbers:

- **Hole shrinkage.** Printed holes come out undersized; a `clearance` parameter (0.2–0.4 mm added to every bore) that you tune once and reference everywhere beats fudging each hole.
- **Wall = multiple of nozzle.** Make wall thickness a parameter and keep it a whole multiple of your nozzle width (e.g. 0.8/1.2/1.6 mm for a 0.4 mm nozzle) so the slicer fills walls cleanly with no thin gap infill.

## Why this compounds

A scripted or spreadsheet-driven library of parts — standoffs, enclosures, sensor mounts — is reusable infrastructure. When the IoT track needs a housing for that ESP32 + SEN5x node, you don't redraw it; you set `board_w`, `board_l`, and `sensor_cutout` and regenerate. Parametric modeling is, in the end, the same instinct as good code: name the things that vary, and let the machine recompute the rest.

**Try next:** turn the standoff above into a function `standoff(height, outer_d, hole_d)` and generate a row of four at different heights with a loop. A parametric part that's also a function is the seed of your own parts library.
