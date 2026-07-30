---
title: "CadQuery 2: parametric code-CAD with a fluent, selector-driven API"
date: 2026-07-30
track: cad-3dprint
summary: "CadQuery models a part as one chained expression: build a solid, select faces or edges with string selectors like >Z and |Z, then place the next feature on them. I walk the Workplane/selector model, contrast the fluent chain with OpenSCAD's CSG and build123d's builder/algebra APIs, and model a parametric mounting plate that exports STEP and STL."
reading_time: 5
tags: [cadquery, code-cad, parametric, python, 3d-printing, selectors]
sources:
  - title: "Introduction — CadQuery Documentation"
    url: "https://cadquery.readthedocs.io/en/latest/intro.html"
  - title: "Selectors Reference — CadQuery Documentation"
    url: "https://cadquery.readthedocs.io/en/latest/selectors.html"
  - title: "cadquery on PyPI"
    url: "https://pypi.org/project/cadquery/"
  - title: "CadQuery/cadquery on GitHub"
    url: "https://github.com/cadquery/cadquery"
  - title: "Examples — CadQuery Documentation"
    url: "https://cadquery.readthedocs.io/en/latest/examples.html"
---

I already keep notes here on OpenSCAD and on build123d, so this entry is the third leg of the Python/code-CAD stool: **CadQuery**. It sits on the same BREP kernel as build123d but reaches for a very different grip — a jQuery-style *fluent* chain where a part is one long expression and the interesting trick is that each step selects the geometry the next step acts on. That selection language is the whole reason to learn it.

## What CadQuery is

CadQuery is a parametric CAD library for Python built on OCCT — the OpenCASCADE kernel — through the OCP bindings. Same kernel family as build123d, so you get real boundary-representation solids with faces, edges, and vertices you can query and fillet, plus proper STEP import/export rather than a triangle soup. The current release is **2.8.0** (June 2026), it requires Python 3.11+, and the project lives at `CadQuery/cadquery` on GitHub. The docs are blunt about the OpenSCAD comparison: OpenCASCADE brings NURBS, splines, surface sewing, STL repair, and STEP I/O that CGAL (OpenSCAD's engine) simply does not have.

Three code-CAD tools, three philosophies, one sentence each:

- **OpenSCAD** describes a solid as a CSG expression tree — `union`/`difference` of primitives in its own DSL — and meshes the result.
- **build123d** drives the same OCP kernel but through context-manager *builders* (or an operator *algebra*), where you accumulate geometry into a running total.
- **CadQuery** drives that kernel through a *fluent chain* on a `Workplane`, where string selectors pick faces/edges and the next call operates relative to that selection.

## The Workplane and the stack

The mental model is a stack. `cq.Workplane("XY")` starts you on a plane; each method consumes what's on the stack and pushes its result back, so you keep chaining. `.box(...)` pushes a solid. `.faces(">Z")` replaces the stack with the top face. `.workplane()` establishes a fresh sketch plane *on that face*, so the next `.hole()` or `.rect()` is located relative to it — no absolute coordinates to bookkeep. This is the "locate features based on other features" efficiency the docs advertise, and it's what keeps CadQuery scripts short.

## String selectors: how the chain picks geometry

Selectors are the payload. Passed as strings to `.faces()`, `.edges()`, or `.vertices()`, they filter the candidate set:

| Selector | Meaning |
|---|---|
| `>Z` / `<Z` | the face/edge farthest in +Z / -Z |
| `+Z` / `-Z` | faces whose **normal** points along +Z / -Z |
| `\|Z` | edges (or face normals) **parallel** to Z |
| `#Z` | perpendicular to Z |
| `%Plane` / `%Circle` | filter by geometry **type** |

They compose with `and`, `or`, `not`, and `exc` (except). `.edges("|Z and >Y")` is "the vertical edge that is also farthest in +Y." When positional selectors get ambiguous you can also `.tag("name")` a point in the chain and refer back to it later with the `tag=` keyword — handy once upstream edits start reshuffling which face is "on top." The contrast with OpenSCAD is stark: OpenSCAD has *no* persistent handle on a face at all, and FreeCAD's GUI equivalent (`Face6`) is exactly the topological-naming fragility CadQuery's *computed* selectors are meant to dodge.

## A parametric mounting plate

Here is a real, runnable part: a rounded-corner mounting plate with a central cable pass-through and four counterbored M4 bolt holes, the whole thing driven by named variables. Note there is no `for` loop for the holes — a construction rectangle plus `.vertices()` fans the one `cboreHole` call out to all four corners.

```python
import cadquery as cq

# --- Parameters (mm) ---
plate_l  = 80.0    # plate length  (X)
plate_w  = 60.0    # plate width   (Y)
plate_t  = 5.0     # plate thickness
corner_r = 6.0     # rounded vertical corners
bolt_d   = 4.5     # M4 clearance hole
cbore_d  = 8.0     # counterbore diameter for the cap head
cbore_h  = 4.0     # counterbore depth
bolt_dx  = 64.0    # bolt spacing along X
bolt_dy  = 44.0    # bolt spacing along Y
cable_d  = 16.0    # central cable pass-through

plate = (
    cq.Workplane("XY")
    .box(plate_l, plate_w, plate_t)   # base solid, centred on the origin
    .edges("|Z")                      # the four vertical edges ...
    .fillet(corner_r)                 # ... rounded into printable corners
    .faces(">Z")                      # step onto the top face
    .workplane()                      # new sketch plane, on that face
    .hole(cable_d)                    # central pass-through (cuts through all)
    .faces(">Z")                      # top face again (now with a hole)
    .workplane()
    .rect(bolt_dx, bolt_dy,           # construction rect = the bolt pattern
          forConstruction=True)
    .vertices()                       # one location at each corner
    .cboreHole(bolt_d, cbore_d, cbore_h)   # all four holes from one call
)

# --- Export: precise BREP for CAD hand-off, mesh for the slicer ---
plate.export("mount_plate.step")
cq.exporters.export(plate, "mount_plate.stl",
                    tolerance=0.001, angularTolerance=0.1)
```

Read the chain top to bottom and it narrates itself: box, round the vertical edges, hop to the top, drill the middle, hop to the top again, lay down a construction rectangle, and counterbore its corners. Change `bolt_dx`/`bolt_dy` and the pattern moves; bump `plate_t` and the counterbore still lands correctly because it is measured from the *selected* top face, not from an absolute Z.

## Exporting for the printer

`plate.export("mount_plate.step")` detects the format from the extension and writes an exact STEP file — reopen it in FreeCAD or KiCad's 3D viewer with curves intact. `cq.exporters.export(..., tolerance=0.001, angularTolerance=0.1)` meshes the solid to STL for the slicer; tighten those two tolerances if you see facets on the fillets. Because the part is a pure function of the variables at the top, re-running is the entire build step, and diffing two revisions is diffing two text files.

If you want a GUI while you iterate, **CQ-editor** is the project's companion IDE: live-reload viewport, a CadQuery stack inspector, a step debugger, and direct STEP/STL export — useful precisely because selectors are easier to *learn* when you can see which face lit up.

**Try next:** Add a `.tag("top")` right after the first `.faces(">Z")`, then replace the second `.faces(">Z")` with `.workplaneFromTagged("top")` and re-run — a quick way to feel how tags make a chain robust against upstream edits that would otherwise change which face `>Z` resolves to.
