---
title: "Infill Patterns Compared: Gyroid, Cubic, and Why Rectilinear Won't Die"
date: 2026-08-27
track: cad-3dprint
summary: "Infill patterns are toolpath generation algorithms, and the useful taxonomy splits them by two mechanical properties: whether the pattern is a 2D tile repeated on every layer or a true 3D structure whose cross-section changes with Z, and whether extrusion paths cross inside a layer. This article walks the generation mechanics of rectilinear, grid, cubic and gyroid, explains what crossing paths do to flow and nozzle collisions, and surveys what published strength testing does — and does not — establish."
reading_time: 7
tags: [infill, slicing, fdm, gyroid, toolpath, mechanical-testing]
sources:
  - title: "Prusa Knowledge Base — Infill patterns"
    url: "https://help.prusa3d.com/article/infill-patterns_177130"
  - title: "Materials (2022) — Investigation of Tensile Properties of Different Infill Pattern Structures of 3D-Printed PLA Polymers"
    url: "https://pmc.ncbi.nlm.nih.gov/articles/PMC9331637/"
  - title: "Polymers (2024) — Optimization of Tensile Strength and Cost-Effectiveness of PETG in FDM Using the Taguchi Method and ANOVA"
    url: "https://pmc.ncbi.nlm.nih.gov/articles/PMC11597993/"
---

**Gist.** An infill pattern is not a texture; it is a toolpath generation algorithm, and its two load-bearing properties are whether the pattern is a two-dimensional tile stamped identically onto every layer or a genuine three-dimensional structure whose cross-section changes with height, and whether the generated paths cross each other inside a single layer. Those two properties determine strength anisotropy, print time, and extrusion consistency respectively. The cost of the "better" patterns is real: a three-dimensional, non-crossing pattern such as gyroid buys near-isotropic support at the price of continuous curvature that slows the motion system, which is why the oldest pattern in the menu — rectilinear — remains the default for speed.

## Two axes of classification

Slicer menus present a dozen patterns as peers. Mechanically there are two questions to ask of each.

**Is the pattern 2D or 3D?** A 2D pattern — rectilinear, grid, triangles, honeycomb — is computed once as a planar tiling and repeated on every layer, at most rotated by a fixed angle between layers. The result is a set of vertical walls: extruded roads stack directly on top of the roads below, forming continuous membranes running in Z. A 3D pattern — cubic, adaptive cubic, gyroid, 3D honeycomb — is defined as a volume, and each layer receives the **horizontal cross-section of that volume at its own Z height**. Slicing a lattice of corner-standing cubes, or a gyroid surface, yields a curve that is different on every layer; the walls it builds are tilted or curved rather than vertical.

The mechanical consequence follows directly. Vertical membranes are stiff against loads in their own plane — vertical compression, and horizontal tension along the line direction — and weak against horizontal shear across them, where the load is carried only by layer-to-layer bonds. A 3D pattern's tilted walls decompose any load direction into components partly carried along extruded roads, which is the origin of the **"equal strength in all directions"** claim the Prusa documentation makes for gyroid. The claim is directional, not absolute: isotropy means no weak axis, not a higher peak than a 2D pattern loaded along its strong axis.

**Do paths cross within a layer?** Grid draws two perpendicular line families in the same layer; every intersection deposits material where material already sits. Rectilinear draws one family per layer and rotates 90° on the next, so **no path in a layer ever crosses another**. Gyroid cross-sections are families of non-intersecting waves. Cubic, as sliced, produces crossing paths per layer.

## What crossing costs

A crossing is a flow disturbance. The nozzle arrives at a point where the previous pass already left a road at full height, and one of two things happens: the new road is squeezed over the old one, locally doubling the deposited volume, or the nozzle tip strikes the solidified crossing. The Prusa documentation records the audible symptom — grid infill produces noise at crossings from material accumulation — and at high speeds the accumulated bumps grow layer over layer until the **nozzle catches on a raised intersection**, which on a bed-slinger can shift layers and on any machine can knock the part loose. The mitigation slicers apply, reducing infill flow or accepting the collisions, trades density accuracy for reliability.

Non-crossing patterns avoid the entire failure mode: rectilinear and gyroid can be printed at higher volumetric flow with consistent extrusion, because every road lands either on air-gap-bridging infill of the previous layer or on clean lower geometry, never on a same-layer lump. This — not strength — is the strongest argument for gyroid on fast printers, and the strongest argument for rectilinear ever since there were printers at all.

## Why rectilinear stays the default

Rectilinear's toolpath is the degenerate case that motion systems love: **long straight segments at constant velocity, joined by sharp turns only at the perimeter**. Acceleration limits are irrelevant along a straight line; the extruder runs at a steady rate; the path length per unit of covered area is minimal. Gyroid's cross-section is continuous curvature, which the slicer emits as many short segments; the motion planner must respect centripetal acceleration limits through every arc, so the average speed drops and the G-code size grows. On input-shaper-era firmware the gap has narrowed but not closed. The Prusa documentation lists rectilinear and grid as the fastest patterns, and rectilinear additionally as the reference for material consumption — patterns such as honeycomb consume roughly 25% more material for the same nominal density.

Cubic occupies a middle position: straight segments (fast), 3D structure (no continuous vertical shear planes), but crossing paths. Its derivative **adaptive cubic** changes the algorithm rather than the geometry — the interior is filled with an octree whose cells double in size with distance from any wall, so density is high where skin needs support and sparse in the deep interior. Prusa's documentation credits it with material savings of about 25% against rectilinear at equivalent support of the top surfaces, which for large parts converts directly into time.

### The gyroid, precisely

The gyroid is a triply periodic minimal surface (TPMS): an infinite smooth surface with zero mean curvature everywhere, repeating in all three axes, that divides space into two congruent interpenetrating channels. It is approximated by the level set

**sin x · cos y + sin y · cos z + sin z · cos x = 0**

A slicer does not mesh this surface. It evaluates the implicit function on each layer plane — fix z, and the equation becomes a curve in x and y — and extracts the level-set contour as the infill path for that layer. The period of the trigonometric terms is scaled so that the wall spacing matches the requested density. Two properties fall out of the mathematics for free: the per-layer contours are **smooth, non-self-intersecting wave trains**, and successive layers' contours shift phase continuously, so the printed walls lean and twist but always overlap the layer below. A side benefit follows from the two-channel topology: both channels are connected, so resin, air, or dissolvable support material can flow through the entire interior, which is why gyroid parts drain and why the pattern appears in heat-exchanger research.

## What the published testing supports

The literature is larger than it is consistent, and three honest findings survive it.

**Density dominates pattern.** A 2024 Taguchi/ANOVA study on polyethylene terephthalate glycol (PETG) in *Polymers* found infill density the most influential factor on tensile strength, contributing **48% of the variance** across its parameter set — more than any geometric factor studied. Arguing gyroid versus cubic at 15% infill while ignoring the option of 25% rectilinear optimizes the smaller term.

**Pattern rankings do not transfer between load cases.** A 2022 study in *Materials* tensile-tested five patterns in polylactic acid (PLA) at 20–80% density and found **honeycomb strongest (13.79 MPa) and gyroid weakest (8.56 MPa)** of the set, attributing honeycomb's advantage to larger bonded contact area between adjacent roads. Other studies, in other materials and load cases, place gyroid at the top. Both results can be true: a uniaxial tensile coupon rewards whatever pattern happens to align material with the pull axis, and a pattern with no weak direction has no aligned direction either.

**Isotropy is gyroid's defensible claim; peak strength is not.** No published result establishes gyroid as strongest in a single well-aligned load direction. The defensible statement is weaker and more useful: gyroid degrades least when the load direction is unknown or changes, which describes most printed brackets and none of the standard test coupons.

## Pitfalls

- **Grid at high flow rates causes nozzle strikes**: material accumulates at same-layer crossings until the nozzle catches a raised intersection and shifts or detaches the part.
- **Comparing patterns at equal nominal density compares unequal material**: honeycomb-family patterns consume roughly 25% more filament than rectilinear at the same setting, so a strength win may be a mass win.
- **Gyroid G-code inflates file size and starves weak motion buffers**: the curved contours are emitted as many short segments, and older 8-bit boards stutter when the planner queue drains.
- **A tensile coupon overstates 2D patterns**: the standard flat specimen loads infill along the layer plane where vertical membranes are strongest, a geometry few real parts share.
- **Switching pattern to fix a strength problem usually fixes the wrong variable**: published ANOVA work puts density's contribution near half the variance, ahead of any pattern effect.
