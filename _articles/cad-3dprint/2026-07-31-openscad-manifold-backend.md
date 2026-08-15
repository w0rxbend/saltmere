---
title: "OpenSCAD's Manifold backend: booleans without the Nef tax"
date: 2026-07-31
track: cad-3dprint
summary: "Why CGAL's Nef polyhedra crawl on boolean-heavy CSG, what Emmett Lalish's Manifold library changes, and how to enable and benchmark it from the command line."
reading_time: 6
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

**Gist.** A final render (F6) of a boolean-heavy constructive solid geometry (CSG) model in OpenSCAD can take minutes, because the classic backend evaluates every boolean exactly, in arbitrary-precision rational arithmetic, on a single thread. The Manifold backend replaces that representation with guaranteed-manifold triangle meshes in single-precision floating point, evaluated in parallel, and the OpenSCAD maintainers' benchmark suite records speedups of roughly 5–30x. The cost is the loss of exactness: input that CGAL tolerated silently can surface as an error, and the backend must be selected explicitly because CGAL remains the default.

## Why CGAL Nef polyhedra are slow

The classic geometry engine is the Computational Geometry Algorithms Library (CGAL), and specifically its **Nef polyhedron** representation. Nef polyhedra model solids as combinations of half-spaces evaluated in **exact rational arithmetic**. The representation is robust by construction: no floating-point cracks, no ambiguity where faces are coincident. The robustness is paid for in two compounding ways.

First, **every coordinate is an arbitrary-precision rational, so its bit-length grows as operations are chained.** A `union()` over a `difference()` over an `intersection()` carries the accumulated precision of the entire subtree beneath it. The cost of a boolean is therefore not constant in the size of its operands alone; it depends on the depth and history of the CSG tree that produced them.

Second, **the classic pipeline is single-threaded.** A model containing hundreds of mutually independent booleans — subtractions that touch disjoint regions of the solid and could in principle be evaluated concurrently — is processed one boolean after another on one core. Deeply nested trees combine both effects: each boolean is exact, serial, and progressively heavier than the last.

## What Manifold changes

Manifold is a standalone geometry library written by **Emmett Lalish**. It is not specific to OpenSCAD; Blender, Godot and BRL-CAD use it as well.

Its design inverts CGAL's trade-offs on three axes:

- **Guaranteed-manifold triangle meshes.** The output of every operation is a watertight, topologically valid solid by construction — the property a 3D-printing toolchain requires of an exported mesh.
- **Single-precision floats rather than rationals.** Coordinates are **fixed-size**, so per-coordinate cost does not grow as the CSG tree deepens. The bit-length growth described above disappears; what disappears with it is exactness.
- **Parallelism through Threading Building Blocks (TBB).** Independent booleans are distributed across available cores rather than serialised.

Per the OpenSCAD maintainers' own benchmark suite, Manifold runs roughly **5–30x faster** than the CGAL fast-csg path, with individual boolean-heavy models reported well above that range. The magnitude for any given model depends on how boolean-heavy its tree is; a model dominated by a single large mesh import has little for either backend to parallelise.

## Selecting the backend

Manifold ships in OpenSCAD **nightly and development snapshots**, where it was promoted from experimental to a supported backend. As of 2026-07-31 the most recent tagged stable release remains 2021.01, which predates the work, so a development snapshot is required. **CGAL remains the default**, and Manifold is opt-in.

In the graphical interface the setting is **Preferences → Advanced → 3D Rendering → Backend → "Manifold"**. In earlier snapshots the same capability appeared under Preferences → Features as a `manifold` flag; that path has been removed.

From the command line, pass `--backend=manifold`. The older `--enable=manifold` Features flag is deprecated in favour of `--backend`.

The scope of the flag is narrow and worth stating precisely: **`--backend` controls final geometry — the F6 render and mesh export — and does not affect the OpenCSG preview reached with F5.** A model whose preview is responsive while export is slow is exactly the case the flag addresses; a model whose *preview* is slow is bounded by something else.

## A benchmark model

The following model is deliberately boolean-heavy: a plate drilled and pocketed by a grid of cylinders, so that the CSG tree contains hundreds of mutually independent subtractions.

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

Rendering it under both backends isolates the difference in wall-clock time:

```bash
# CGAL (default)
time openscad -o cgal.stl --backend=cgal perforated.scad

# Manifold
time openscad -o manifold.stl --backend=manifold perforated.scad
```

Raising `N` widens the gap: the count of subtractions grows as N², and each additional boolean costs the CGAL path more than the last because of accumulated precision, while the Manifold path costs a fixed-precision operation divided across cores.

Verification matters before the result is trusted for production export. Comparing the **triangle count and bounding box** of `cgal.stl` against `manifold.stl` establishes that the two backends produced the same solid, not merely two files of similar size.

## Where the exactness was load-bearing

The single-precision representation is the source of both the speedup and the only behavioural regression. **Degenerate or self-intersecting input that CGAL's exact arithmetic resolved without complaint can surface as an error under Manifold.** Such geometry — zero-thickness walls, faces that touch exactly, surfaces that cross themselves — was fragile independently of the backend; the exact representation concealed the fragility rather than removing it.

## Pitfalls

- **Preview time is unchanged after switching backends.** `--backend` governs the F6 render and export path; F5 preview goes through OpenCSG, which the flag does not touch.
- **A model that rendered under CGAL fails under Manifold.** Single-precision evaluation surfaces degenerate or self-intersecting geometry that exact rational arithmetic absorbed silently.
- **Setting `--enable=manifold` appears to do nothing on a current snapshot.** That Features flag is deprecated; `--backend=manifold` is the current selector, and the corresponding entry under Preferences → Features has been removed.
- **Installing the latest tagged stable release does not provide the backend.** The most recent tag is 2021.01, which predates the backend; Manifold requires a development snapshot.
- **Assuming the backend is active because it was enabled once.** CGAL is the default, so a fresh profile, a different machine, or a script that omits `--backend` reverts to the slow path without any message saying so.
- **Treating a benchmark speedup as transferable.** The reported 5–30x range comes from boolean-heavy suite models; a tree with few booleans has little for parallel evaluation to exploit.
