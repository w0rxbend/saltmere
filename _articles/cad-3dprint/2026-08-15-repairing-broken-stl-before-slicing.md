---
title: "Repairing a Broken STL Before Slicing: admesh, Manifold Meshes, and the Mesh-vs-BREP Divide"
date: 2026-08-15
track: cad-3dprint
summary: "A slicer refuses a model, or prints a part with a phantom hole in one wall. The STL is geometrically invalid — non-manifold edges, unfilled holes, flipped normals, or self-intersections. This article covers diagnosis and repair from the command line with admesh, the cases that require MeshLab or Blender's 3D-Print Toolbox instead, and why a parametric boundary-representation source cannot enter these states at all."
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

**Gist.** The stereolithography (STL) format describes a solid as an unindexed list of triangles, so solidity is not stored — it is an *invariant* that holds only when every edge is shared by exactly two consistently wound triangles. When the invariant breaks, the file remains a valid STL but no longer denotes a printable volume, and the slicer either refuses it or fills the wrong side of a wall. Repair tools restore the invariant by welding near-coincident vertices and patching boundary loops, at the cost of altering geometry the author never authored: a filled hole is a flat triangle fan, not the intended surface.

## Why triangle soup tears

An STL triangle is stored as three vertex coordinate triples plus a normal vector, with **no shared vertex index and no explicit edge records**. Two triangles are "the same edge" only if their coordinates agree. The watertightness invariant is therefore: *every edge is incident to exactly two facets, and the two facets traverse that edge in opposite directions* (consistent winding). Four defect classes violate it:

- **Non-manifold edges** — an edge incident to one facet (an open boundary) or to three or more (a fin). The mesh no longer partitions space into an inside and an outside, so no ray-crossing-parity test can classify a point.
- **Holes** — missing facets leave closed boundary loops of one-sided edges. The enclosed volume is undefined along those loops.
- **Flipped normals** — a facet wound in the opposite sense declares its outward face inward. The slicer's inside/outside classification inverts locally, producing missing or inverted walls.
- **Self-intersections** — facets pass through one another, common after boolean unions on tessellated inputs. Every edge may still have exactly two neighbours, so the mesh can be *manifold and still not bound a coherent volume*.

The last case is why "manifold" and "printable" are not synonyms, and why the edge-count report below can read clean on a model that still slices wrongly.

Floating-point representation compounds the problem. Two facets that were generated from the same conceptual vertex may store coordinates differing in the final mantissa bits. Exact comparison then fails, the edge is counted once rather than twice, and a crack appears in the topology that is invisible in a rendered view.

## Diagnosis with admesh

**admesh** is a command-line tool and C library for processing triangulated solid meshes, packaged in most distributions (`apt install admesh`, `brew install admesh`). Invoked with no repair flags it reports counts only:

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

The report maps directly onto the invariant. **Disconnected edges** are edges that failed to find a partner facet — holes or floating-point cracks. **Backwards edges** are edges whose two facets traverse them in the *same* direction, that is, inconsistent winding. **Degenerate facets** are triangles of zero area, which have no well-defined normal and no partner-matching behaviour worth relying on.

## The repair pipeline

admesh applies a fixed sequence rather than a user-ordered one: an **exact** edge match on identical coordinates, then a **nearby** pass that connects facets whose vertices lie within a tolerance, then the optional unconnected-facet removal, hole filling, and normal correction. No repaired file is written unless an output flag names one: `--write-ascii-stl=` or `--write-binary-stl=`.

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

| Flag | Effect |
|---|---|
| `--nearby` | Connect facets whose vertices are within tolerance |
| `--tolerance=` | Distance for the nearby check (model units, i.e. mm) |
| `--iterations=` | Repeat the nearby pass this many times |
| `--remove-unconnected` | Delete facets with zero neighbours |
| `--fill-holes` | Add facets to close open boundary loops |
| `--normal-directions` | Fix inconsistent triangle winding (CW/CCW) |
| `--normal-values` | Recompute the stored normal vectors |
| `--write-binary-stl=` | Write a binary STL (smaller than ASCII) |

The two normal flags address different data. `--normal-directions` corrects the *winding* — the vertex order that defines which side faces outward — while `--normal-values` recomputes the *stored* normal vector from that winding. Requesting value recomputation without direction correction leaves the stored vectors consistent with a winding that is itself wrong, which removes the mismatch without removing the defect.

Re-running the plain diagnostic on the output is the verification step: a repaired solid reports **0 disconnected edges and 0 backwards edges**. Two limits remain. admesh **does not resolve self-intersections**, and its hole filling closes a boundary loop with facets spanning the gap — appropriate for a one-facet crack, not for a genuinely missing region, where the result is a flat patch rather than the intended surface.

## Cases requiring a mesh processor

**MeshLab**, and its Python binding **PyMeshLab**, provide filters for duplicate-vertex merging, non-manifold edge repair, and hole closure, which makes a repair pass reproducible as a script rather than a sequence of clicks:

```python
import pymeshlab
ms = pymeshlab.MeshSet()
ms.load_new_mesh("broken.stl")
ms.meshing_remove_duplicate_vertices()
ms.meshing_repair_non_manifold_edges()
ms.meshing_close_holes(maxholesize=30)
ms.save_current_mesh("fixed.stl")
```

The `maxholesize` bound is the deliberate part: it caps closure at loops below a facet count, leaving larger openings untouched rather than spanning them with an arbitrary surface.

**Blender's 3D-Print Toolbox** add-on is the interactive counterpart. Its **Check All** operation reports non-manifold edges, flipped normals, intersecting faces, and zero-area faces, and it offers **Make Manifold** together with select-by-fault operations that isolate each reported defect in the viewport. That selection step is the value: it identifies *which* wall is inverted before a repair strategy is chosen.

## Repairing the source instead

Every defect above is a property of **mesh** geometry — triangles with no first-class notion of face, edge, or volume. A **boundary representation (BREP)** model, the form FreeCAD builds internally and a STEP file stores, describes the solid as trimmed analytic surfaces stitched along shared edges, with topology maintained by the modelling kernel. A BREP cylinder is a cylinder: a non-manifold edge or a backwards normal is not a representable state of that model, so it cannot arise. Tessellation into triangles happens once, at export.

The consequence is a routing rule. When a broken STL originated from a locally held CAD model, the shorter path is re-export rather than repair — adjusting the tessellation setting (in FreeCAD, the mesh deviation) and regenerating the file from a representation that was never invalid. Mesh repair earns its place where no source exists: a downloaded model, a 3D scan, or a sculpt authored directly as triangles.

## Pitfalls

- **A clean admesh report does not imply a printable model.** Self-intersecting facets can leave every edge with exactly two neighbours, so disconnected- and backwards-edge counts both read zero while the enclosed volume remains undefined.
- **An over-large `--tolerance` welds distinct features together.** The nearby pass connects any facets whose vertices fall inside the radius, so a tolerance approaching the thinnest wall merges the two sides of that wall into one surface.
- **`--fill-holes` on a large opening produces a flat span, not the missing surface.** The symptom is a printed part with a planar face where a curve belonged; the cause is that boundary-loop closure has no information about the geometry that was removed.
- **`--normal-values` without `--normal-directions` hides the defect rather than fixing it.** Stored normals recomputed from a backwards winding agree with that winding, so the facet no longer reports a normal-value mismatch while still facing inward.
- **Vertices that differ only in the last mantissa bits fail exact matching.** The mesh renders as closed while the diagnostic reports disconnected edges, because topology is inferred from coordinate equality rather than stored.
- **`--remove-unconnected` deletes geometry silently.** Facets with zero neighbours are discarded, which removes stray debris and equally removes a small legitimate shell that failed to connect for tolerance reasons.
