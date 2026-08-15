---
title: "CadQuery 2: parametric code-CAD with a fluent, selector-driven API"
date: 2026-07-30
track: cad-3dprint
summary: "CadQuery models a part as one chained expression: build a solid, select faces or edges with string selectors such as >Z and |Z, then place the next feature relative to the selection. This entry walks the Workplane stack and selector language, contrasts the fluent chain with OpenSCAD's CSG and build123d's builder/algebra APIs, and models a parametric mounting plate that exports STEP and STL."
reading_time: 6
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

**Gist.** A parametric part must place each feature relative to geometry produced by earlier features, and hard-coded absolute coordinates break as soon as an upstream dimension changes. CadQuery solves this with a fluent chain over a `Workplane` stack: **each call consumes the current stack contents and pushes its result back**, and string selectors such as `>Z` or `|Z` recompute which faces or edges the next operation acts on. The cost is that the selection is *positional* and recomputed on every run, so an upstream edit that changes which face is highest silently redirects a downstream feature.

## Position among code-CAD tools

CadQuery is a parametric computer-aided design (CAD) library for Python built on OCCT — the OpenCASCADE kernel — through the OCP bindings. That kernel yields boundary-representation (BREP) solids whose faces, edges and vertices remain queryable and filletable, and it supports exact STEP import and export rather than a triangle mesh only. The current line is CadQuery 2.x, distributed on the Python Package Index (PyPI) as `cadquery`; the repository is `CadQuery/cadquery` on GitHub. The supported Python versions are those listed on the PyPI page for the release being installed. The documentation states the OpenSCAD contrast directly: OpenCASCADE provides NURBS, splines, surface sewing, STL repair and STEP input/output that CGAL, OpenSCAD's engine, does not.

Three tools, three models:

- **OpenSCAD** describes a solid as a constructive solid geometry (CSG) expression tree — `union`/`difference` over primitives in a dedicated domain-specific language — and meshes the result.
- **build123d** drives the same OCP kernel through context-manager *builders*, or an operator *algebra*, accumulating geometry into a running total.
- **CadQuery** drives that kernel through a *fluent chain* on a `Workplane`, where string selectors pick faces or edges and the next call operates relative to that selection.

## The Workplane stack

The model to hold in mind is a stack of topological entities. `cq.Workplane("XY")` establishes a starting plane. **Every chained method consumes the current stack and pushes its result**, which is what permits arbitrary-length chains without intermediate variables:

- `.box(...)` pushes a solid.
- `.faces(">Z")` replaces the stack with the single face farthest along +Z.
- `.workplane()` establishes a new sketch plane *on the entity currently on the stack*, so a subsequent `.hole()` or `.rect()` is expressed in that face's local coordinates.

The invariant that makes parametric edits cheap is that **a feature placed after `.faces(">Z").workplane()` is measured from the selected face, not from an absolute Z**. Changing the plate thickness moves the sketch plane with the face; the counterbore depth stays correct without editing any other line.

## String selectors

Selectors are passed as strings to `.faces()`, `.edges()` or `.vertices()` and filter the candidate set:

| Selector | Meaning |
|---|---|
| `>Z` / `<Z` | the face or edge farthest in +Z / −Z |
| `+Z` / `-Z` | faces whose **normal** points along +Z / −Z |
| `\|Z` | edges (or face normals) **parallel** to Z |
| `#Z` | perpendicular to Z |
| `%Plane` / `%Circle` | filter by geometry **type** |

They compose with `and`, `or`, `not` and `exc` (except): `.edges("|Z and >Y")` denotes the vertical edge that is also farthest in +Y.

Because the filter is evaluated against the current geometry, the result is **computed rather than stored**. This is the difference from a graphical-CAD reference such as FreeCAD's `Face6`, which names a face by an index in the kernel's topology list — an index that can be reassigned when an upstream operation is edited. OpenSCAD offers no persistent handle on a face at all. CadQuery's positional selectors avoid a stale index but introduce their own dependency: **the selection is a function of geometry, so geometry changes can change the selection**. For that case the chain supports `.tag("name")` at a point of interest and later reference through the `tag=` keyword or `.workplaneFromTagged("name")`, which pins the reference to the tagged step instead of re-resolving a positional query.

## A parametric mounting plate

A rounded-corner mounting plate with a central cable pass-through and four counterbored M4 bolt holes, driven entirely by named variables. There is no `for` loop over the holes: a construction rectangle plus `.vertices()` places four locations on the stack, and the single `cboreHole` call applies at each.

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
cq.exporters.export(plate, "mount_plate.step")
cq.exporters.export(plate, "mount_plate.stl",
                    tolerance=0.001, angularTolerance=0.1)
```

Two constraints are load-bearing in that chain. First, `corner_r` must remain below half the shorter plate dimension, since `fillet` fails when the requested radius cannot be inscribed against the adjacent edges. Second, `bolt_dx`/`bolt_dy` must keep the counterbores clear of both the fillets and the central hole; the arithmetic is not checked by the library, and an overlapping counterbore either produces a degenerate solid or a fillet that the kernel refuses to build.

## Export

`cq.exporters.export(plate, "mount_plate.step")` infers the format from the file extension and writes an exact STEP file, reopenable in FreeCAD or KiCad's 3D viewer with curved surfaces intact. `cq.exporters.export(..., tolerance=0.001, angularTolerance=0.1)` tessellates the solid to STL for a slicer: **`tolerance` bounds the linear deviation between the mesh and the true surface, and `angularTolerance` bounds the angular step**, so visible faceting on fillets is addressed by reducing them, at the cost of triangle count and file size.

Because the model is a pure function of the variables at the top of the file, rebuilding is re-running the script, and comparing two revisions is a text diff.

**CQ-editor** is the project's companion integrated development environment: a viewport that re-renders on save, an object inspector over the resulting geometry, a debugger, and direct STEP/STL export. The object inspector is the part that matters while learning selectors, since it shows which entities a given filter resolved to.

**Try next:** insert `.tag("top")` immediately after the first `.faces(">Z")`, replace the second `.faces(">Z")` with `.workplaneFromTagged("top")`, and re-run — this demonstrates the difference between a pinned reference and a positional query that re-resolves against changed geometry.

## Pitfalls

- **A later operation changes which face is farthest in +Z.** Adding a boss or raising a region above the original top face makes a subsequent `.faces(">Z")` resolve to the new geometry, and the feature lands on the boss instead of the plate. Tag the intended face and use `.workplaneFromTagged`.
- **A selector matches more entities than intended, and the operation applies to all of them.** `.faces("+Z")` matches every face whose normal points along +Z, not only the topmost one; a hole call then drills each match.
- **A selector matches nothing and the chain fails downstream, not at the selector.** An empty stack surfaces as an error inside the operation that consumed it, so the reported line is the `.hole()` or `.fillet()`, not the `.faces()` that produced the empty set.
- **`fillet(corner_r)` fails when the radius exceeds what the adjacent faces admit.** The kernel reports a construction failure rather than clamping the radius, so a parameter sweep that raises `corner_r` past the plate's half-width breaks the build.
- **`forConstruction=True` is omitted from the bolt-pattern rectangle.** The rectangle is then treated as real geometry and participates in the solid instead of serving only as a source of vertex locations.
- **STL export tolerances are left at values too coarse for small fillets.** Faceting visible in the slicer originates in the tessellation parameters, not in the BREP model, so the STEP file remains exact while the mesh does not.
- **A dimension is edited in one place but duplicated elsewhere in the script.** Since parametric behaviour rests on every derived value tracing back to the named variables, any literal repeated inline drops out of the parametric relationship silently.
