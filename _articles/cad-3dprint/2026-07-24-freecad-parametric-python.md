---
title: "Regenerable parametric parts: driving FreeCAD from Python"
date: 2026-07-24
track: cad-3dprint
summary: "Parametric computer-aided design reshapes a whole part from one number. FreeCAD's Python console makes that a script that can be versioned, diffed, and reused."
reading_time: 5
tags: [freecad, parametric, python, 3d-printing, cad]
sources:
  - title: "FreeCAD scripting basics"
    url: "https://wiki.freecad.org/FreeCAD_Scripting_Basics"
  - title: "FreeCAD Spreadsheet workbench"
    url: "https://wiki.freecad.org/Spreadsheet_Workbench"
---

**Gist.** A hand-drawn bracket is one bracket: every dimension is a literal, and changing the wall thickness means redrawing the holes, fillets and mounting tabs by hand. Parametric computer-aided design (CAD) replaces those literals with **named parameters that the geometry is recomputed from**, so a single edit propagates to every dependent feature. The cost is that the model becomes a program with a dependency graph — it must be recomputed to be correct, and a parameter change that violates a constraint fails at recompute time rather than being silently absorbed by the drawing.

## The Python console exposes the modelling API

FreeCAD is open source and scriptable: most operations the graphical interface performs issue Python calls, and the same calls are available from **View → Panels → Python console**. A parametric standoff — a tube used to space one board off another — is a few lines:

```python
import FreeCAD as App, Part

doc = App.newDocument("standoff")
height, outer_d, hole_d = 12.0, 8.0, 3.2   # the parameters

body = Part.makeCylinder(outer_d/2, height)
bore = Part.makeCylinder(hole_d/2, height)
part = body.cut(bore)

Part.show(part)
doc.recompute()
```

Changing `hole_d` from `3.2` to `4.3` (M3 clearance to M4 clearance) and re-running regenerates the part. The load-bearing property is not brevity but that **the model's inputs are text**: the change is a one-line diff under version control, and the history of the part is inspectable by the same tools as the history of code.

Two mechanisms are visible in the snippet and worth separating, because they behave differently.

**Direct shape construction.** `Part.makeCylinder` and `Shape.cut` build a boundary-representation solid immediately. The result is a shape object, not a recipe: `Part.show` places the finished geometry in the document. Re-running the script rebuilds from scratch; there is no stored link back to `hole_d`. The parameter lives in the script, and the script is the only thing that knows how to regenerate.

**Document objects and recompute.** `doc.recompute()` is the other mechanism. A FreeCAD document holds objects in a **dependency graph**, and recompute walks that graph to bring objects marked as touched back into agreement with their inputs. When a part is built from parametric document features rather than from a one-shot shape, editing a property touches the feature and every downstream consumer, and recompute re-evaluates them in dependency order.

The distinction matters when a script is re-run against an existing document: shapes produced by `Part.show` accumulate as independent objects, because nothing ties a new shape to the one the previous run produced.

## Spreadsheet-driven models

For parts handed to someone who will not edit Python, the **Spreadsheet workbench** provides the same single-source-of-truth property without a script. Cells are given **aliases** — `wall`, `bolt_d`, `count` — and sketch constraints and feature properties reference them as expressions such as `Spreadsheet.wall`.

The consequence is structural rather than cosmetic. An expression establishes a **dependency edge from the consuming feature to the spreadsheet object**, so editing an aliased cell touches every feature that references it, and recompute re-evaluates exactly those features. The table becomes the model's input surface: one place to read the current configuration, one place to change it.

## Print parameters belong in the parameter set

Parametric intent covers manufacturing constraints, not only geometry. Two are worth naming explicitly.

**Bore clearance.** Printed holes come out undersized relative to the modelled diameter. A single `clearance` parameter — on the order of 0.2 to 0.4 mm added to every bore — that is tuned once and referenced everywhere keeps the correction in one place. The alternative, adjusting each hole's literal diameter after a test print, distributes one physical fact across every feature that has a hole.

**Wall thickness as a multiple of nozzle width.** Keeping wall thickness a whole multiple of the nozzle width (0.8, 1.2 or 1.6 mm for a 0.4 mm nozzle) lets the slicer fill the wall with complete extrusions. A thickness that is not a multiple leaves a residual gap narrower than one extrusion, which the slicer must fill with thin-wall or gap-fill logic rather than a clean perimeter.

Both are parameters, not constants embedded in geometry, and both change when the printer or nozzle changes.

## Reuse

A scripted or spreadsheet-driven set of parts — standoffs, enclosures, sensor mounts — is reusable because the varying quantities are named. A housing for an ESP32 plus SEN5x sensor node is produced by setting `board_w`, `board_l` and `sensor_cutout` and regenerating, rather than by redrawing. The discipline is the same one that makes code reusable: name what varies, and let the machine recompute what depends on it.

The standoff above extends directly to a function:

```python
def standoff(height, outer_d, hole_d):
    body = Part.makeCylinder(outer_d/2, height)
    bore = Part.makeCylinder(hole_d/2, height)
    return body.cut(bore)

for i, h in enumerate([6.0, 8.0, 10.0, 12.0]):
    s = standoff(h, 8.0, 3.2)
    s.translate(App.Vector(i * 12.0, 0, 0))   # avoid coincident solids
    Part.show(s)
doc.recompute()
```

The translation is not decoration: without it the four solids occupy the same coordinates, which is legal but leaves overlapping geometry that is difficult to select and ambiguous to export.

## Pitfalls

- **Re-running a `Part.show` script against an open document duplicates objects.** Each call adds a new object; nothing links the new shape to the previous run's, so the document accumulates stacked solids that look like one part in the viewport.
- **Editing a property without recomputing leaves the document stale.** The object is marked touched and its old shape is still what is displayed and exported, so a scripted change can appear to have no effect until `doc.recompute()` runs.
- **A parameter change that violates a sketch constraint fails at recompute, not at edit time.** The error surfaces on the dependent feature, which may be several steps downstream of the value that was changed.
- **Hole diameters modelled at nominal size print undersized.** A bore drawn at 3.2 mm does not accept an M3 screw once printed; the clearance allowance is a modelling parameter, not a slicer setting.
- **Wall thickness that is not a whole multiple of nozzle width leaves a sub-extrusion residue.** The symptom is gap-fill or thin-wall extrusions inside what was intended as a solid perimeter.
- **Aliases removed or renamed in a spreadsheet break every expression referencing them.** The dependent features fail on the next recompute rather than at the moment the alias is removed.
