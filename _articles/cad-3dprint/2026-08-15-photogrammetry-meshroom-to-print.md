---
title: "Photogrammetry to Printable Part: Meshroom, Mesh Cleanup, and Getting Scale Right"
date: 2026-08-15
track: cad-3dprint
summary: "Turning 60 phone photos into a printable replacement part is a real workflow now: Meshroom 2025.1 runs the AliceVision pipeline end-to-end, but the raw mesh it emits is a million-triangle, hollow, arbitrarily-scaled shell. The work is in the capture discipline and the cleanup — decimation, hole closing, scaling against a reference marker, and making the thing watertight before the slicer sees it."
reading_time: 6
tags: [photogrammetry, meshroom, alicevision, blender, meshlab, 3d-printing, reverse-engineering]
sources:
  - title: "Meshroom 2025.1.0 release (alicevision/Meshroom, GitHub)"
    url: "https://github.com/alicevision/Meshroom/releases/tag/v2025.1.0"
  - title: "Meshroom.org — Release 2025.1 announcement"
    url: "https://meshroom.org/index.php/2025/08/18/release-2025-1/"
  - title: "CG Channel — Epic Games releases RealityScan 2.0 and RealityScan Mobile 1.7"
    url: "https://www.cgchannel.com/2025/06/epic-games-releases-realityscan-2-0-and-realityscan-mobile-1-7/"
  - title: "RealityScan 2.0 announcement (realityscan.com)"
    url: "https://www.realityscan.com/news/realityscan-20-new-release-brings-powerful-new-features-to-a-rebranded-realityscan"
  - title: "COLMAP — Structure-from-Motion and Multi-View Stereo (GitHub)"
    url: "https://github.com/colmap/colmap"
---

Some parts can't be measured with calipers and [rebuilt in FreeCAD](/articles/cad-3dprint/2026-07-24-freecad-parametric-python/): a snapped bracket with compound curves, a discontinued appliance knob, a hand-carved original. Photogrammetry — reconstructing 3D geometry from overlapping photos — gets you a mesh from nothing but a phone camera. Getting from that mesh to something a slicer will accept is the part most tutorials skip.

## Capture: where 80% of quality is decided

Reconstruction works by matching feature points across photos, so everything that helps matching helps the model:

- **Overlap aggressively.** 60–70% overlap between consecutive shots; orbit the part in two or three rings at different heights, 20–40 photos minimum for a small object, more for anything with occlusions.
- **Texture is signal.** Matte, textured surfaces reconstruct beautifully. Shiny, transparent, or uniformly-colored parts reconstruct as noise — specular highlights move between frames and break matching. The standard cheats: dust the part with foot spray or chalk powder, or scribble pencil marks on featureless plastic.
- **Lock the lighting.** Diffuse, even light (overcast sky, or a light tent). Move the camera, never the object — or if you must rotate the object on a turntable, use a featureless background so the software can't "see" the static room.
- **Include a scale reference.** Photogrammetry has no idea how big anything is. Put a ruler, a printed checkerboard, or any object of precisely known dimension in the scene touching the same surface. You'll use it later.

## The pipeline: Meshroom, or the alternatives

[Meshroom](https://github.com/alicevision/Meshroom/releases/tag/v2025.1.0) is the open-source workhorse — a node-graph GUI over the AliceVision framework. The 2025.1.0 release (August 2025) was a major one: a reworked node system with dedicated pipelines, so you drop photos in, pick the photogrammetry pipeline, and hit Start. Under the hood it runs feature extraction, structure-from-motion (camera pose recovery), depth-map estimation (this stage wants an NVIDIA GPU with CUDA), meshing, and texturing, and emits an OBJ. On a mid-range GPU a 60-photo dataset takes tens of minutes.

Alternatives worth knowing: **RealityScan** is Epic's rebrand of RealityCapture — the desktop app became [RealityScan 2.0 in June 2025](https://www.cgchannel.com/2025/06/epic-games-releases-realityscan-2-0-and-realityscan-mobile-1-7/), unified in name with the mobile capture app (RealityScan Mobile), and stays free for individuals and small teams under Epic's revenue threshold. It's faster than Meshroom and the phone app makes capture nearly idiot-proof, but it's closed-source and Windows-only on desktop. [COLMAP](https://github.com/colmap/colmap) is the academic reference implementation — command-line SfM/MVS, excellent camera poses, commonly used as the front end for Gaussian-splatting workflows; more knobs, less hand-holding.

## Cleanup: from shell to solid

The raw mesh is unusable for printing: a few million triangles, holes where the camera never saw (the underside), floating debris from the background, and dimensions in arbitrary units. MeshLab and Blender split the work.

**Delete the junk.** In MeshLab or Blender, select and delete the ground plane and disconnected islands (MeshLab: *Remove Isolated Pieces*).

**Decimate.** A slicer is happy with 100–500k triangles. Blender's Decimate modifier at ratio 0.1, or MeshLab's *Simplification: Quadric Edge Collapse Decimation*, preserves shape remarkably well. Do this early; every later operation gets faster.

**Scale to reality.** This is the step that makes it a *part* instead of a *prop*. Measure your reference object in the mesh (MeshLab's measuring tool, or Blender with `N`-panel dimensions) and scale uniformly:

```
scale_factor = real_dimension_mm / measured_dimension_in_mesh
```

In Blender: select all, `S`, type the factor, then *Object → Apply → Scale*. Verify by measuring a *second* known feature — if the ruler says 100 mm and the bolt hole spacing is also right, your calibration holds. Expect dimensional accuracy in the 0.2–0.5 mm range at best from phone photos; anywhere a precise fit matters (a bore, a mating face), plan to model that feature explicitly.

**Close and solidify.** The scan is an open shell. MeshLab's *Close Holes* handles small gaps; for the missing underside, Blender is better — delete the ragged boundary, fill with `F`/grid fill, or boolean the shell against a box to give it a flat, printable base. If the part needs wall thickness (a scanned shell you want to reprint as a shell), Blender's Solidify modifier adds it. The goal is a watertight, manifold mesh; the corpus piece on [repairing broken STLs before slicing](/articles/cad-3dprint/2026-08-15-repairing-broken-stl-before-slicing/) covers the verification and repair tooling, and everything there applies doubly to scan meshes.

**Hybrid remodel when it matters.** For mechanical parts the strongest pattern is scan-as-reference: import the cleaned mesh into FreeCAD or Blender, and model the *functional* surfaces (holes, flats, mounting bosses) as clean parametric geometry aligned to the scan, keeping the organic surfaces from the mesh. You get sub-scan accuracy exactly where the part interfaces with something else.

## Slice it

Export STL or [3MF](/articles/cad-3dprint/2026-08-11-3mf-vs-stl-3d-printing/) and treat it like any organic model: scan-derived parts rarely have nice flat faces in useful orientations, so expect [tree supports](/articles/cad-3dprint/2026-08-14-orcaslicer-tree-supports/) and pick the orientation that puts the reconstructed (least accurate) surface where tolerance doesn't matter. Print a first article, caliper it against the original, and rescale by the measured error — one iteration usually lands it.

**Try next:** photograph a broken part 40 times next to a 123-block or ruler, run it through Meshroom 2025.1, and take it through decimate → scale → solidify → slice; caliper the print against the original and note where the 0.3 mm scan error actually shows up.
