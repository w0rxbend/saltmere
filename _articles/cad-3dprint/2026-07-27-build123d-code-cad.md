---
title: "build123d: Parametric Code-CAD in Python for the ESP32 Bench"
date: 2026-07-27
track: cad-3dprint
summary: "build123d is a Pythonic boundary-representation CAD library on the OpenCascade kernel. A comparison of its Builder and Algebra APIs, a contrast with OpenSCAD's CSG DSL, and a parametric ESP32 enclosure lid exported to STEP and STL."
reading_time: 6
tags: [build123d, code-cad, parametric, python, 3d-printing, cadquery]
sources:
  - title: "build123d documentation (readthedocs)"
    url: "https://build123d.readthedocs.io/en/latest/index.html"
  - title: "gumyr/build123d on GitHub"
    url: "https://github.com/gumyr/build123d"
  - title: "build123d on PyPI"
    url: "https://pypi.org/project/build123d/"
  - title: "Key Concepts (builder mode) — build123d docs"
    url: "https://build123d.readthedocs.io/en/latest/key_concepts_builder.html"
  - title: "CadQuery documentation"
    url: "https://cadquery.readthedocs.io/en/latest/"
---

**Gist.** A family of near-identical parts — ESP32 sensor lids differing only in vent pattern and screw spacing — is expensive to maintain as one mouse-drawn CAD document per variant, because a change to a shared dimension has to be replayed by hand in each. build123d expresses the part as a **Python function of named parameters** driving the OpenCascade boundary-representation (BREP) kernel, so a revision is a text diff and a re-run. The cost is that geometry is no longer selected by clicking it: **faces and edges must be identified by queries** (position, area, axis order) that can silently bind to a different face when a parameter changes shape topology.

## What build123d is, and what it is not

build123d is a Python CAD library built on the OpenCascade geometric kernel through the OCP bindings. That one sentence carries the distinctions that matter.

OpenSCAD is a constructive solid geometry (CSG) system with its own domain-specific language: primitives are unioned and differenced, and the result is meshed. build123d instead drives a **BREP kernel**, where a part is a solid carrying explicit faces, edges and vertices. Those entities are addressable: they can be queried, filleted, and written out as **STEP**, a format that preserves exact curve and surface definitions rather than a triangulated approximation. The practical consequence is that a build123d part survives a round trip into downstream CAD or computer numerical control (CNC) toolpathing, whereas a mesh does not.

Because the modelling language is ordinary Python, the standard library, `numpy`, unit-test frameworks and editor tooling apply without adaptation. The model is a program; the usual program-handling machinery works on it.

CadQuery shares the same kernel and an overlapping community. build123d began as a sibling effort by Roger Maitland (gumyr) and emphasises two things: location arithmetic, and a choice between two APIs. It is maintained at `gumyr/build123d` and distributed on the Python Package Index (PyPI) as `build123d`; the current release number and the range of supported Python versions are recorded in that package's metadata rather than fixed here, since both move.

## Builder API versus Algebra API

build123d offers two syntaxes over the identical kernel; the choice is ergonomic, not semantic.

The **Builder API** uses context managers holding an implicit running total. `with BuildPart() as part:` opens a scope, and each object created inside is fused into — or subtracted from — the builder's accumulated state according to its `mode`. The invariant is that **the builder owns exactly one current shape**, and every statement in the block is a transition on it. This reads top to bottom like a procedure, at the cost of state that is not named anywhere in the source.

The **Algebra API** removes the contexts and composes with operators. `Plane.XZ * Pos(X=5) * Rectangle(1, 1)` places a rectangle: **locations multiply, solids combine with `+` and `-`**. Multiplication is location composition and associates left to right, so the plane and the offset combine into one location that then places the rectangle, and the result is a value rather than a mutation. Carrying almost no hidden state makes this form suitable for functions that return parts and for reuse of subassemblies.

## A parametric ESP32 lid

The part below is a snap-in enclosure lid: a top plate, a plug lip that drops into the box, four M3 counterbored corner holes, and cooling slots over the microcontroller. Every dimension is a named parameter.

```python
from build123d import *

# --- Parameters (mm) ---
lid_l, lid_w = 70, 50      # outer footprint
wall        = 2.0          # top-plate thickness
lip_h       = 4.0          # depth the lip drops into the box
lip_inset   = 1.5          # lip offset from the outer edge
screw_d     = 3.2          # M3 clearance hole
head_d      = 6.0          # counterbore for the screw head
hole_inset  = 5.0          # corner-hole offset
vent_len, vent_w = 22, 2   # each cooling slot

with BuildPart() as lid:
    # Top plate
    with BuildSketch():
        RectangleRounded(lid_l, lid_w, radius=3)
    extrude(amount=wall)

    # Lip that plugs down into the enclosure (built on the z=0 face)
    with BuildSketch():
        RectangleRounded(lid_l - 2 * lip_inset, lid_w - 2 * lip_inset, radius=2)
        RectangleRounded(lid_l - 2 * lip_inset - 2 * wall,
                         lid_w - 2 * lip_inset - 2 * wall,
                         radius=1, mode=Mode.SUBTRACT)
    extrude(amount=-lip_h)

    top = lid.faces().sort_by(Axis.Z)[-1]

    # Four M3 counterbored corner holes
    with Locations(top):
        with GridLocations(lid_l - 2 * hole_inset,
                           lid_w - 2 * hole_inset, 2, 2):
            CounterBoreHole(radius=screw_d / 2,
                            counter_bore_radius=head_d / 2,
                            counter_bore_depth=1.2)

    # Cooling vents over the ESP32
    with BuildSketch(top):
        with GridLocations(6, 0, 4, 1):
            SlotOverall(vent_len, vent_w, rotation=90)
    extrude(amount=-wall, mode=Mode.SUBTRACT)

export_step(lid.part, "esp32_lid.step")
export_stl(lid.part, "esp32_lid.stl")
```

Three mechanisms carry the model.

**Location contexts position the next object rather than transforming a finished one.** `Locations` and `GridLocations` establish a set of placements in scope; each object constructed inside the block is instantiated once per placement. This is why a single `CounterBoreHole` call yields four holes: the grid supplies four locations, and the object statement runs against all of them.

**Face selection is a query against the current solid.** `lid.faces().sort_by(Axis.Z)[-1]` collects the faces, orders them along the Z axis, and takes the last — the highest. The result is an ordinary object bound to `top` and reused as both a location source and a sketch plane. The binding is positional, not nominal: it names whichever face happens to sort highest at that point in the build, which is where parameter changes can retarget it.

**`Mode.SUBTRACT` turns any object into a cut.** The same construction statement adds or removes material depending on its mode, so the lip's inner rectangle and the vent slots both remove, using the same syntax as the additive statements around them.

## Exporting for the printer

`export_step(shape, path)` writes the BREP STEP file, default unit `Unit.MM`, reopenable in FreeCAD or KiCad's 3D viewer with curves intact. `export_stl(shape, path, tolerance=0.001, angular_tolerance=0.1)` triangulates the solid for the slicer; the two tolerances bound linear deviation and angular deviation of the mesh from the exact surface, so visible faceting on rounded corners is addressed by lowering them. Both functions report success by returning a boolean rather than by raising on failure, so the return value has to be inspected.

The parametric payoff is contained in the re-run: changing `lid_l`, `lid_w` or the vent count and executing the script produces a new STL without interactive work, and comparing two revisions is comparing two text files.

**Try next:** rewrite the vent block in the Algebra API (`lid -= Pos(...) * extrude(SlotOverall(...), -wall)`) and compare the exported STL against the Builder-API output — a direct way to observe where the two APIs converge.

## Pitfalls

- **A face query silently retargets when a parameter changes topology.** `sort_by(Axis.Z)[-1]` selects whichever face is highest at that moment; if a later dimension change makes the lip or a boss taller than the top plate, the vents are cut into the wrong face and the script still succeeds.
- **`Mode.SUBTRACT` on an object that does not intersect the current shape removes nothing and reports nothing.** A vent grid placed outside the plate footprint yields a solid lid with no error.
- **Export failure is a return value, not an exception.** A script that ignores the boolean from `export_step` or `export_stl` proceeds to the next step while no usable file was written.
- **STL tolerances are geometric, not visual.** `tolerance` and `angular_tolerance` are separate arguments bounding different quantities, so tightening the linear one alone can leave faceting on a small fillet unchanged.
- **Builder state is implicit.** Inside a `BuildPart` block there is no named variable holding the intermediate solid, so an object created in the wrong nesting level fuses into a different builder than intended.
- **STEP and STL are not interchangeable outputs of one model.** STEP preserves exact surfaces; STL is a triangulation produced under the tolerances given, so a slicer's view of the part is never the kernel's view of it.
