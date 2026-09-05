---
title: "Slicing STEP Instead of STL: What OrcaSlicer Does With a BREP"
date: 2026-08-27
track: cad-3dprint
summary: "OrcaSlicer and Bambu Studio import STEP files directly and tessellate them with OpenCASCADE at load time, exposing linear and angular deflection as import parameters. This moves the mesh-resolution decision from the CAD export dialog into the slicer — but the slicer still operates on the resulting triangles, so modifiers, repair, and painting remain mesh operations. The article explains what a boundary representation stores that a mesh discards, derives how linear deflection maps to chord error and segment count, and marks where the workflow falls back to mesh semantics."
reading_time: 7
tags: [step, brep, orcaslicer, tessellation, chord-error, 3d-printing]
sources:
  - title: "Import and Export — OrcaSlicer Wiki (general_settings/import_export)"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/import_export"
  - title: "Open CASCADE Technology user guide: Mesh (BRepMesh)"
    url: "https://occt3d.com/dev/doc/overview/html/occt_user_guides__mesh.html"
  - title: "STEP file manual import quality control — OrcaSlicer issue #9794"
    url: "https://github.com/SoftFever/OrcaSlicer/issues/9794"
  - title: "CNC Kitchen meshStep — STEP to watertight mesh conversion library"
    url: "https://github.com/CNCKitchen/meshStep"
---

**Gist.** A Standard for the Exchange of Product model data (STEP) file stores a boundary representation (BREP): trimmed analytic surfaces stitched along shared edges, in which a cylinder is a cylinder rather than a prism of triangles. OrcaSlicer and Bambu Studio accept STEP files and tessellate them at import with OpenCASCADE, exposing the two deflection parameters that control chord error — so the resolution decision that used to live in the CAD export dialog now lives in the slicer, reversible at every re-import. The cost is that the exactness ends at the import dialog: everything downstream — slicing, modifier meshes, repair, seam and support painting — still operates on the triangles the tessellator produced.

## What a BREP stores that a mesh discards

An STL file is an unindexed list of triangles: three coordinate triples and a normal per facet, no shared vertices, no edges, no faces, no units. A BREP, the form a STEP file serializes (ISO 10303-21, application protocols AP203/AP214/AP242), stores the solid at a higher level:

- **Analytic and freeform surfaces.** A face is a plane, cylinder, cone, sphere, torus, or a non-uniform rational B-spline (NURBS) patch, trimmed by boundary curves. Curvature is represented exactly, not sampled.
- **Explicit topology.** Faces meet along shared edge records, edges meet at shared vertices. Watertightness is a stored property maintained by the modelling kernel, not an invariant that a list of triangles may or may not satisfy.
- **Assembly structure and units.** A STEP file can hold multiple named solids with placements; both slicers expose this as a split-into-parts option at import, so per-part print settings survive the transfer. Millimetres are declared in the file rather than assumed.

The practical consequence: **a STEP file has no resolution**. The question "how many segments per circle" is undefined until something tessellates it, and the slicer is now that something. Exporting STL from computer-aided design (CAD) software bakes one answer into the file forever; exporting STEP defers the answer to import time, where it can be revisited without a round trip to the CAD seat.

## The import pipeline

Both slicers link OpenCASCADE Technology (OCCT), the open-source geometry kernel, and run its BRepMesh algorithm on the imported shape. The OCCT user guide describes the process in two load-bearing stages:

1. **Edge discretization.** Every BREP edge is sampled into a polyline according to the deflection parameters, each edge processed once using its 3D curve and the associated 2D parametric curves of its adjacent faces.
2. **Face triangulation.** The discretized edges form closed contours in each face's parameter space, and a triangulation algorithm fills the interior with triangles that also respect the deflection bounds.

Because each shared edge is discretized **once** and both adjacent faces reuse the same polyline, the resulting mesh is watertight by construction — the cracks, T-junctions, and disconnected edges that plague exported STL cannot arise from this path. The mesh-repair pipeline that a downloaded STL routinely needs has nothing to do here. (The same edge-welding argument underlies independent implementations: CNC Kitchen's meshStep library, which reimplements STEP tessellation outside OCCT, samples each shared BREP edge once precisely to guarantee closed 2-manifold output, and reports 97.2% watertight results across the 10,000-model ABC dataset.)

OrcaSlicer surfaces the two BRepMesh knobs in a dialog shown at STEP import (re-enabled, if dismissed, under Preferences → "Show the STEP mesh parameter setting dialog"):

- **Linear deflection** — the maximum distance allowed between the original surface and its polygonal approximation, in model units (mm). This is the **chord error** of the mesh.
- **Angular deflection** — the maximum angle between subsequent segments of the approximating polyline, hence between adjacent facet normals.

The two bounds compose: a segment must satisfy both. Linear deflection alone lets a large-radius arc be spanned by long chords (the sagitta stays small); angular deflection caps how far the surface normal may rotate across one facet regardless of radius, which is what keeps big cylinders from showing visible flats.

## From deflection to segment count

For a circular arc of radius *r*, a chord subtending angle *θ* deviates from the arc by the sagitta *h* = *r*(1 − cos(*θ*/2)). Inverting for a chord-error budget *h*:

**θ = 2·arccos(1 − h/r)**, so a full circle needs **n = ⌈2π/θ⌉ segments**, and for small *h/r* the small-angle expansion gives *θ* ≈ 2·√(2*h*/*r*), i.e. **n grows as √(r/h)**. Halving the chord error costs only √2 more segments — resolution is cheap in facet count, which is why aggressive linear deflection values remain tractable. The angular bound then takes over on large radii: with an angular deflection *α*, a circle needs at least ⌈2π/*α*⌉ segments no matter how loose the linear bound is.

### Implementation sketch (Scala)

The interaction of the two bounds fits in a few lines:

```scala
import scala.math.*

/** Segments a full circle of radius r needs to satisfy both bounds. */
def circleSegments(r: Double, linDefl: Double, angDefl: Double): Int = {
  // Chord of angle theta has sagitta h = r * (1 - cos(theta/2)).
  val thetaLinear  = 2.0 * acos(max(-1.0, 1.0 - linDefl / r))
  val theta        = min(thetaLinear, angDefl) // both bounds must hold
  ceil(2 * Pi / theta).toInt
}

// linear deflection 0.05 mm, angular deflection 0.5 rad:
// r =   2 mm -> 15 segments   (linear bound governs)
// r =  50 mm -> 70 segments
// r = 200 mm -> 140 segments  (n ~ sqrt(r/h) growth)
```

Running the sketch shows the regime change: small fillets are governed by the linear bound and get few segments; large cylinders accumulate segments as √*r* until the angular bound flattens the growth. A 0.05 mm chord error is well below a 0.4 mm nozzle's positioning contribution, so tightening far past that adds triangles the extruded bead cannot express.

## Where the workflow falls back to mesh semantics

The exact geometry does not survive past import. Once BRepMesh has run, the slicer holds triangles, and every downstream feature has mesh semantics:

- **Slicing itself** intersects a plane with triangles, producing polygons whose arcs are already polylines. A slicer that emits G2/G3 arc moves does so by *re-fitting* arcs to those polylines — it does not read the cylinder from the BREP.
- **Modifier meshes, negative parts, and boolean operations** act on the tessellation. A modifier region clips against facets, so its boundary lands on chord positions, not on the analytic surface.
- **Seam, support, and fuzzy-skin painting** store per-facet paint. The paintable granularity is the triangle produced at import; a coarse import gives coarse paint boundaries.
- **The 3MF project file saves the mesh**, not the BREP. Re-tessellating at a different deviation means re-importing the STEP file and re-doing placement and per-object settings.

The import dialog is therefore a **one-way door per project**: the deviation chosen at import is the ceiling on geometric fidelity for everything that session does. OrcaSlicer issue #9794 documents the failure mode of getting it wrong silently — a user traced periodic vertical artifacts on printed parts to reduced mesh quality of the STEP import path, and asked for exactly the manual quality control the dialog now provides.

## Choosing values

No published benchmark ties deflection values to measured print accuracy across slicers, so the honest guidance is bound-based. Chord error adds to, and should sit below, the other radial error sources: extrusion-width variation and motion-system error, each typically on the order of several hundredths of a millimetre on a well-tuned machine. A linear deflection in the low hundredths of a millimetre puts tessellation error beneath the process noise; values approaching a tenth of a millimetre become visible as flats on convex surfaces near the build-plate-facing first layers, where light rakes across the facets. Angular deflection is what protects large radii — the OCCT guide's typical default range is 12–20 degrees for viewport meshing, and import for printing sits far tighter than that.

## Pitfalls

- **The 3MF project stores the tessellation, not the STEP.** Reopening a project and tightening "import quality" is impossible; the fix is re-importing the STEP file, which discards placement and painting done since.
- **Linear deflection alone leaves visible flats on large cylinders.** The sagitta of a long chord on a 100 mm radius stays within a loose linear bound while the facet normals swing degrees apart; the angular bound is what caps the normal swing.
- **Tessellation error is invisible in the preview at default zoom.** Shaded rendering interpolates normals across facets, so a mesh that will print visible flats can render smooth; the wireframe view shows the actual chords.
- **Boolean and modifier boundaries land on chords.** A negative volume meant to stop exactly at a curved wall clips triangles, so the cut surface inherits the import chord error rather than the CAD surface.
- **Painting resolution is facet resolution.** Seam or support paint applied to a coarsely imported model snaps to large triangles, and repainting after a finer re-import starts from zero.
- **A STEP import that is watertight by construction can still slice wrongly if the source solid is bad.** Shared-edge welding prevents cracks introduced by tessellation, but a self-intersecting or open solid in the CAD file passes its defects through; the guarantee covers the conversion, not the model.
