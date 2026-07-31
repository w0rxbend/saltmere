---
title: "OpenSCAD's Manifold backend: booleans without the Nef tax"
date: 2026-07-31
track: cad-3dprint
summary: "Why CGAL's Nef polyhedra crawl on boolean-heavy CSG, what Emmett Lalish's Manifold library changes, and how to enable it plus benchmark it from the CLI."
reading_time: 5
tags: [openscad, cad, 3d-printing, manifold, cgal, rendering]
sources:
  - title: "OpenSCAD announcement: Manifold in nightly builds"
    url: "https://fosstodon.org/@OpenSCAD/113256867413539398"
  - title: "Mailing list: Manifold backend is no longer experimental"
    url: "https://lists.openscad.org/empathy/thread/D6KV3ZLXHLBHSITSQ5GPUZUKHURU4ABE"
  - title: "Manifold library (Emmett Lalish)"
    url: "https://github.com/elalish/manifold"
  - title: "kintel/openscad-benchmark"
    url: "https://github.com/kintel/openscad-benchmark"
  - title: "Hackaday: Faster OpenSCAD Rendering Is On The Horizon"
    url: "https://hackaday.com/2023/10/03/at-last-faster-openscad-rendering-is-on-the-horizon/"
---

If a `render()` (F6) on a boolean-heavy model takes minutes, you've hit the CGAL Nef bottleneck. OpenSCAD's Manifold backend removes it, and it's a one-checkbox change in current nightlies.

## Why CGAL Nef polyhedra are slow

OpenSCAD's classic geometry engine is CGAL, and specifically its **Nef polyhedron** representation. Nef polyhedra model solids as intersections of half-spaces using **exact rational arithmetic**. That gives bulletproof robustness — no floating-point cracks, no coincident-face ambiguity — but it's expensive in two compounding ways.

First, every coordinate is an arbitrary-precision rational, so numbers grow in bit-length as you chain operations. A `union()` of a `difference()` of an `intersection()` carries the accumulated precision of all of it. Second, the classic pipeline is **single-threaded**. A model with hundreds of independent booleans processes them one after another on one core while the rest of your CPU sits idle. Deeply nested CSG trees are where this shows up hardest: each boolean is exact, serial, and increasingly heavy.

## What Manifold brings

Manifold is a standalone geometry library by **Emmett Lalish** (started as a Google 20% project, now maintained alongside his work at Wētā FX). It's not OpenSCAD-specific — Blender, Godot, and BRL-CAD use it too.

Its design inverts CGAL's tradeoffs:

- **Guaranteed-manifold triangle meshes.** Every operation's output is a watertight, topologically valid solid by construction — the property 3D printing actually needs — so there's no Nef fallback to repair edge cases.
- **Single-precision floats, not rationals.** Coordinates stay fixed-size, so precision doesn't balloon as the CSG tree deepens.
- **Parallelism via TBB.** Independent booleans run across all your cores.

Per the OpenSCAD maintainers' own benchmark suite, Manifold runs roughly **5–30x faster** than the CGAL fast-csg path — e.g. `maze.scad` from 5m32s to 3.35s (27.7x), `menger.scad` from 3m6s to 5.08s (36.7x). Users have reported swings from 60x to several-hundred-x on their own models. Your mileage depends on how boolean-heavy the tree is.

## Enabling it

Manifold has shipped in OpenSCAD **nightly / development snapshots since build 2024.09.28**, where it was promoted from experimental to a supported backend. As of 2026-07-31 the last tagged stable release is still 2021.01, so you need a snapshot from the 2025.xx line — and CGAL remains the *default*, so Manifold is opt-in.

GUI: **Preferences → Advanced → 3D Rendering → Backend → "Manifold"**. (In earlier snapshots it lived under Preferences → Features as a `manifold` flag; that path is gone.)

CLI: pass `--backend=manifold`. The old `--enable=manifold` Features flag is deprecated — use `--backend`.

## Benchmark it yourself

Here's a deliberately boolean-heavy model — a plate drilled and pocketed by a grid of cylinders, so the CSG tree has hundreds of independent subtractions:

```scad
// perforated.scad
$fn = 48;
N = 14;          // 14 x 14 = 196 booleans
pitch = 8;

difference() {
  cube([N*pitch, N*pitch, 6], center = true);
  for (x = [0:N-1], y = [0:N-1])
    translate([(x - N/2)*pitch + pitch/2,
               (y - N/2)*pitch + pitch/2, 0])
      rotate([0, 0, 45])
        cylinder(h = 20, d = 4, center = true);
}
```

Render it both ways from the command line and compare wall-clock time:

```bash
# CGAL (default)
time openscad -o cgal.stl --backend=cgal perforated.scad

# Manifold
time openscad -o manifold.stl --backend=manifold perforated.scad
```

Bump `N` to 20 or 30 and the gap widens fast — the CGAL run climbs into minutes while Manifold stays in the low seconds. Note that `--backend` controls final geometry (F6 / STL export); it doesn't change OpenCSG *preview* (F5). If your preview looks fine but export is what's slow, this is exactly the knob to turn. One caveat: because Manifold works in single precision, degenerate or self-intersecting input that CGAL silently tolerated can surface as an error — usually a sign the model was fragile to begin with.

**Try next:** run the benchmark above on your slowest real model at `N`-equivalent scale, then diff the two STLs' triangle counts and bounding boxes to confirm Manifold produces the same solid before you trust it for production exports.
