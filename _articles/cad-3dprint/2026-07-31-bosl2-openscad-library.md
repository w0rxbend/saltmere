---
title: "BOSL2: attachments, rounded primitives, and threads in OpenSCAD"
date: 2026-07-31
track: cad-3dprint
summary: "Vanilla OpenSCAD requires tracking every coordinate by hand and reinventing fillets and threads per project. BOSL2 supplies attachment anchors, rounding arguments on primitives, and a threading library, at the cost of a large include, longer render times, and incompatibility with BOSL v1."
reading_time: 5
tags: [openscad, bosl2, code-cad, enclosures, 3d-printing]
sources:
  - title: "BOSL2 — The Belfry OpenSCAD Library v2.0 (GitHub)"
    url: "https://github.com/BelfrySCAD/BOSL2"
  - title: "BOSL2 threading.scad — threaded_rod, screws, bottle/pipe threads (wiki)"
    url: "https://github.com/BelfrySCAD/BOSL2/wiki/threading.scad"
  - title: "Belfry OpenSCAD Library (BOSL2) Brings Useful Parts and Tools Aplenty — Hackaday"
    url: "https://hackaday.com/2025/02/18/belfry-openscad-library-bosl2-brings-useful-parts-and-tools-aplenty/"
---

**Gist.** Plain OpenSCAD offers primitives and transforms and nothing above them: a rounded box with a lid and a threaded hole is assembled from hand-computed `translate()` offsets, a `minkowski()` approximation of a fillet, and a thread module copied from elsewhere. **BOSL2** — the Belfry OpenSCAD Library, version 2.0, which requires OpenSCAD **2021.01 or newer** — replaces those three constructions with attachment anchors, rounding arguments on the primitives themselves, and a threading module. The cost is a large library included into every model, mesh-level thread geometry whose facet count is under the author's control and drives render time, and a deliberate break with BOSL v1: **v1 code does not run under BOSL2**.

## The coordinate-tracking problem

In vanilla OpenSCAD a child's position is an absolute expression evaluated by the author. Placing a 3 mm lid on top of a 25 mm box means writing `translate([0, 0, 25/2 + 3/2])` when the box is centred, or `translate([0, 0, 25])` when it is not — and every one of those expressions has to be revisited when a wall thickness or a height changes. The failure mode is silent: the model still renders, the lid is merely 1.5 mm into the box, and the error surfaces on the print bed rather than in the compiler.

BOSL2's attachment system removes the arithmetic by making placement **relative and named** rather than absolute and numeric. Every BOSL2 shape exposes anchor points — `TOP`, `BOTTOM`, `LEFT`, `RIGHT`, `FRONT` (also spelled `FWD`), `BACK` — together with `spin` (rotation about the anchor's own axis) and `orient` (which way the attached child points). A child inside an `attach()` block is positioned by the parent's geometry, so **the invariant is that the relationship survives a change to the parent's dimensions**:

```scad
include <BOSL2/std.scad>

wall = 2;
box  = [60, 40, 25];

cuboid(box, rounding = 3, edges = "Z") {   // round only the vertical edges
    attach(TOP)   cuboid([box.x, box.y, wall]);  // lid, flush on the top face
    attach(FRONT) cyl(h = 4, d = 12);            // boss on the front face
}
```

Changing `box` moves the lid and the boss with it. No offset in the file needs editing, because no offset was written.

## Rounding without Minkowski

`minkowski()` produces a fillet by sweeping a sphere over a solid. It is correct and it is expensive: the operation is a convolution over the two meshes, and a sphere with a usable facet count applied to a box makes preview and render times climb sharply. It is also indiscriminate — the sphere rounds every edge, including the bottom face, which for a printed part removes the flat first layer and forces either supports or an elephant-foot-style contact patch.

BOSL2 puts rounding in the primitive instead. `cuboid()` accepts a `rounding` argument and an `edges` selector, so `edges = "Z"` rounds the four vertical edges and leaves the top and bottom faces flat — **the geometry a printed enclosure requires**, since the bottom face carries bed adhesion. For shapes the primitives do not cover, `offset_sweep()` rounds the ends of an extrusion and `edge_profile()` runs a custom fillet or chamfer profile along selected edges. The positional shorthands `up(z)`, `left(x)` and `fwd(y)` stand in for the corresponding `translate([...])` calls where an absolute move is still the clearest expression.

## Threads as printed mesh geometry

`threading.scad` provides `threaded_rod()`, `threaded_nut()`, screw utilities, and dedicated bottle- and pipe-thread modules. A threaded hole is a subtraction of a rod generated with `internal = true`:

```scad
include <BOSL2/std.scad>
include <BOSL2/threading.scad>

difference() {
    cuboid([50, 50, 20], rounding = 2, edges = "Z");
    // M8 tapped hole, cut through the full 20 mm and out both faces
    threaded_rod(d = 8, l = 22, pitch = 1.25, internal = true, $fn = 48);
}
```

Two details are load-bearing. First, **`l = 22` exceeds the 20 mm block height on purpose**: a subtracted solid that ends exactly on a face leaves a zero-thickness coincident surface, which CGAL renders as a non-manifold artefact or a visible skin. Overshooting by 1 mm at each end removes the coplanar face entirely.

Second, **`internal = true` generates the thread shape intended to be subtracted** — the mask for a nut or a tapped hole rather than a rod. It does not by itself account for what the printer adds: extrusion width and the slicer's horizontal-expansion setting both narrow the printed aperture, BOSL2 exposes the global `$slop` for that printer-dependent allowance, which the internal-thread modules apply. The remaining variables are the facet resolution (`$fn` and the `$fa`/`$fs` pair, which control how finely the circular cross-section is approximated) and the slicer settings. No single value is correct everywhere: **fit must be established by printing a test coupon at the intended layer height and nozzle diameter**.

The parts library extends past fasteners — gears, hinges, clips, dovetails — but on sensor enclosures threads and attachments are the two modules in constant use.

## Installation and the v1 boundary

BOSL2 is installed as a directory in the OpenSCAD library search path: on Linux, `$HOME/.local/share/OpenSCAD/libraries/BOSL2/`. Models then begin with `include <BOSL2/std.scad>`, and `include <BOSL2/threading.scad>` where threads are needed.

The version boundary is the most common source of confusion. **BOSL v1 and BOSL2 have different APIs, and v1 code does not run under BOSL2.** Snippets found in older forum posts and blog articles will fail with undefined-module or wrong-argument errors rather than degrade gracefully, so the v2 wiki is the only reliable reference. The BOSL2 repository describes the library as beta, so module signatures can change between revisions.

## Pitfalls

- **A subtracted solid whose face is coplanar with the parent's face** renders as a zero-thickness surface: CGAL either reports a non-manifold result or produces a visible film across the opening. Extend the subtracted solid past both faces.
- **`minkowski()` used for a fillet rounds the bottom edge too**, removing the flat first layer and costing bed adhesion. `cuboid(rounding = r, edges = "Z")` rounds only the vertical edges.
- **A high `$fn` on a threaded rod multiplies facet count across every turn of the helix**, so render time grows with thread length as well as with `$fn`. A value that previews acceptably on a 10 mm rod can stall a render on a 60 mm one.
- **A thread cut to nominal dimensions binds.** Extrusion width and the slicer's horizontal-expansion setting narrow the printed aperture; the fit must be calibrated on a test coupon at the production layer height.
- **BOSL v1 examples copied from older sources fail under BOSL2** with undefined-module or argument errors, because the two libraries share a name lineage but not an API.
- **`attach()` places a child relative to the parent's anchor, not relative to the world**, so a transform applied outside the parent moves the whole assembly while a transform written inside an `attach()` block composes with the anchor's orientation.
