---
title: "Repairing a Broken STL Before You Slice: admesh, Manifold, and the Mesh-vs-BREP Divide"
date: 2026-08-15
track: cad-3dprint
summary: "Your slicer refuses a model, or prints a part with a phantom hole in one wall. The STL is broken — non-manifold edges, unfilled holes, flipped normals, or self-intersections. Here's how to diagnose and repair one from the command line with admesh 0.98.5, when to reach for MeshLab or Blender's 3D-Print Toolbox instead, and why a parametric BREP source never has these bugs in the first place."
reading_time: 6
tags: [stl, mesh-repair, admesh, manifold, freecad, 3d-printing]
sources:
  - title: "admesh — CLI and C library for processing triangulated solid meshes (GitHub)"
    url: "https://github.com/admesh/admesh"
  - title: "ADMesh command line tool (readthedocs)"
    url: "https://admesh.readthedocs.io/en/latest/cli.html"
  - title: "ADMesh man page (Ubuntu manpages)"
    url: "https://manpages.ubuntu.com/manpages/bionic/man1/admesh.1.html"
  - title: "Blender 3D-Print Toolbox (Blender Manual)"
    url: "https://docs.blender.org/manual/en/latest/addons/mesh/3d_print_toolbox.html"
  - title: "PyMeshLab documentation"
    url: "https://pymeshlab.readthedocs.io/"
---

A slicer that spits out "the model is not manifold" or quietly leaves a gap in one wall isn't being fussy — it's telling you the STL is geometrically invalid. STL describes a solid as nothing but a list of triangles, so "solid" is an emergent property that holds only if every triangle lines up perfectly with its neighbors. When that invariant breaks, the file is still a valid STL; it just doesn't describe a printable object. Before you throw it at the slicer again, it's worth knowing exactly what broke and how to fix it.

## Why triangle soup tears

STL stores each triangle as three vertices and a normal, with **no shared vertex index** and no explicit edges. A watertight solid requires that every edge be shared by exactly two triangles, each winding in a consistent direction. Four failures routinely violate that:

- **Non-manifold edges** — an edge shared by one triangle (a hole boundary) or three-plus (a fin). The mesh no longer cleanly separates inside from outside.
- **Holes** — missing triangles leave open boundary loops, so the "solid" has gaps.
- **Flipped normals** — a triangle wound backwards points its "outside" face inward. The slicer can't tell which side is solid, giving inverted or missing walls.
- **Self-intersections** — triangles pass through each other (common after careless boolean unions), so there's no coherent volume to fill.

Floating-point noise makes it worse: two triangles that *should* share a vertex may store coordinates that differ in the last bit, leaving a crack that reads as a non-manifold edge even though the geometry looks closed.

## Diagnose and repair with admesh

**admesh** is a small, fast CLI (and C library) that dates to the mid-1990s and is still the quickest way to get a numeric health report on an STL. The current release is **0.98.5**; it's packaged in most distros (`apt install admesh`, `brew install admesh`). Run it with no repair flags to just read the diagnosis:

```console
$ admesh broken.stl
[...]
Number of facets:                  12648
Facets with 1 disconnected edge:      42
Facets with 2 disconnected edges:      6
Facets with 3 disconnected edges:      1
Total disconnected facets:            49
Backwards edges:                       7
Degenerate facets:                     3
```

Non-zero "disconnected edges" means holes or cracks; "Backwards edges" flags inconsistent winding; "Degenerate facets" are zero-area triangles. Now repair. admesh runs a fixed pipeline — an **exact** edge match, then a **nearby** pass that welds vertices within a tolerance, then optional hole-fill and normal fixes — and writes a **binary** STL out:

```bash
admesh \
  --nearby --tolerance=0.001 --iterations=2 \
  --remove-unconnected \
  --fill-holes \
  --normal-directions \
  --normal-values \
  --write-binary-stl=fixed.stl \
  broken.stl
```

What each flag does, straight from the man page:

| Flag | Short | Effect |
|---|---|---|
| `--nearby` | `-n` | Connect facets whose vertices are within tolerance |
| `--tolerance=` | `-t` | Distance for the nearby check (model units, i.e. mm) |
| `--remove-unconnected` | `-u` | Delete facets with zero neighbors |
| `--fill-holes` | `-f` | Add facets to close open boundary loops |
| `--normal-directions` | `-d` | Fix inconsistent triangle winding (CW/CCW) |
| `--normal-values` | `-v` | Recompute the stored normal vectors |
| `--write-binary-stl=` | `-b` | Write a binary STL (smaller than ASCII) |

Re-run the plain `admesh fixed.stl` afterward: a repaired solid should report **0** disconnected edges and **0** backwards edges. admesh is deliberately conservative, though — it welds and patches, but it won't resolve **self-intersections** or reconstruct a genuinely large missing region sensibly. Fill a big hole and you get a flat triangle fan across the gap, not the surface you meant.

## When admesh isn't enough: GUI and scriptable tools

For self-intersections and heavier reconstruction, reach for a mesh processor. **MeshLab** (and its Python binding **PyMeshLab**) exposes filters for merging close vertices, removing self-intersecting faces, and closing holes, so you can script a repeatable repair pass:

```python
import pymeshlab
ms = pymeshlab.MeshSet()
ms.load_new_mesh("broken.stl")
ms.meshing_remove_duplicate_vertices()
ms.meshing_repair_non_manifold_edges()
ms.meshing_close_holes(maxholesize=30)
ms.save_current_mesh("fixed.stl")
```

**Blender's 3D-Print Toolbox** add-on is the interactive counterpart: its **Check All** button reports non-manifold edges, flipped normals, intersecting faces, and zero-area faces, with **Make Manifold** and select-by-fault buttons to walk each defect. It's the right tool when you need to *see* which wall is inverted before deciding how to patch it.

## Repair the source, not the mesh

Here's the deeper point. Every failure above is a property of **mesh** geometry — triangle soup with no notion of a face, edge, or volume as a first-class object. A **BREP** (boundary representation) model, the kind FreeCAD builds and a STEP file stores, is different in kind: it describes the solid as trimmed analytic surfaces stitched along shared edges, with topology the kernel *guarantees* is manifold. A STEP cylinder is a cylinder; it can't develop a non-manifold edge or a backwards normal, because those states aren't representable. The triangle mesh only appears at the very end, when the model is tessellated for the slicer.

So when a broken STL came from *your own* CAD model, the fastest fix is usually not to repair the mesh at all — it's to go back to the parametric source, re-export the STL at a sane tessellation (in FreeCAD, lower the mesh deviation), and skip the corruption entirely. Mesh repair is for the cases where the source is gone: a downloaded model, a scan, or a sculpt. When you *own* the geometry, fix it where it's still a solid, not after it's been ground into triangles.

**Try next:** run `admesh` with no flags on your last failed print's STL, note the "disconnected edges" and "backwards edges" counts, then repair with the full command above and diff the two reports to confirm both dropped to zero before you re-slice.
