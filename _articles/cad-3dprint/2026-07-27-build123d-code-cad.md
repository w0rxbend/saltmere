---
title: "build123d: Parametric Code-CAD in Python for the ESP32 Bench"
date: 2026-07-27
track: cad-3dprint
summary: "build123d is a Pythonic BREP CAD library on the OpenCascade kernel. I compare its Builder and Algebra APIs, contrast it with OpenSCAD's CSG DSL, and model a parametric ESP32 enclosure lid that exports straight to STEP and STL."
reading_time: 5
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

I keep a small pile of ESP32 sensor boards on the bench, and every one of them eventually needs a printed lid. Sketching lids in a mouse-driven CAD program is fine once; doing it eleven times, each with a slightly different vent pattern and screw spacing, is exactly the kind of work a parametric model should absorb. This journal already has notes on FreeCAD's Python console and on OpenSCAD, so this entry is about the tool I have actually settled on: **build123d**.

## What build123d is, and what it is not

build123d is a Python CAD library built on the OpenCascade geometric kernel (via the OCP bindings). That single sentence carries most of the important distinctions. OpenSCAD is a constructive solid geometry (CSG) system with its own domain-specific language: you union and difference primitives, and the result is meshed. build123d instead drives a boundary-representation (BREP) kernel, so a "part" is a real solid with faces, edges, and vertices you can query, fillet, and export as STEP for downstream CAD or CNC work — not just a triangle soup. And because it is ordinary Python, the whole standard library, `numpy`, unit tests, and your editor's tooling are all just there.

If you know CadQuery, build123d will feel familiar: same kernel, overlapping community, even a shared Discord. build123d started as a sibling/successor effort by Roger Maitland (gumyr) and leans harder into two things — location math and a choice of two APIs. The latest release is **0.11.1** (July 2026), it supports Python 3.10 through 3.14, and the repository is actively maintained at `gumyr/build123d`.

## Builder API vs Algebra API

build123d ships two ways to describe the same geometry.

The **Builder API** uses context managers that hold an implicit running total. You open `with BuildPart() as part:`, add objects, and each one is fused (or subtracted) into the builder's state. It reads top-to-bottom like a recipe and is the mode I reach for first.

The **Algebra API** drops the contexts and composes shapes with operators instead. `Plane.XZ * Pos(X=5) * Rectangle(1, 1)` reads as "take a rectangle, move it, place it on the XZ plane" — locations multiply, solids add and subtract with `+` and `-`. It carries almost no hidden state, which makes it pleasant for functions that return parts. Both target the identical kernel, so it is purely an ergonomics choice.

## A parametric ESP32 lid

Here is a real Builder-API part: a snap-in enclosure lid with a plug lip, four M3 counterbored corner holes, and a set of cooling vents over the microcontroller. Every dimension is a named parameter at the top.

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

A few things worth noticing. `Locations` and `GridLocations` are location contexts — they position the *next* objects rather than transforming after the fact, which is why the four holes come from one `CounterBoreHole` call. `sort_by(Axis.Z)[-1]` is a live query against the solid's faces, selecting the top plate to sketch the vents onto. And `Mode.SUBTRACT` turns any object into a cut, so the lip's inner rectangle and the vent slots both remove material.

## Exporting for the printer

The two export calls do the whole job. `export_step(shape, path)` writes a precise BREP STEP file (default `Unit.MM`) that you can reopen in FreeCAD or KiCad's 3D viewer with curves intact. `export_stl(shape, path, tolerance=0.001, angular_tolerance=0.1)` meshes the solid for the slicer — tighten the tolerances if you see facets on rounded corners. Both return a boolean success flag.

Change `lid_l`, `lid_w`, or the vent count and re-run: a new STL drops out in a second, no clicking. That is the entire reason to model in code — the part is a function of its parameters, and diffing two revisions is diffing two text files.

**Try next:** Rewrite the vent block in the Algebra API (`lid -= Pos(...) * extrude(SlotOverall(...), -wall)`) and confirm the exported STL is byte-for-byte equivalent — a fast way to feel where the two APIs converge.
