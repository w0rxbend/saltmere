---
title: "Photogrammetry to Printable Part: Meshroom, Mesh Cleanup, and Scale Recovery"
date: 2026-08-15
track: cad-3dprint
summary: "Meshroom 2025.1 runs the AliceVision photogrammetry pipeline end-to-end over a set of phone photographs, but the mesh it emits is a million-triangle, hollow, arbitrarily-scaled shell. The engineering effort lies in capture discipline and in cleanup: decimation, hole closing, scale recovery against a reference of known dimension, and making the surface watertight before the slicer sees it."
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

**Gist.** Some parts cannot be measured with calipers and [rebuilt in FreeCAD](/articles/cad-3dprint/2026-07-24-freecad-parametric-python/) — a snapped bracket with compound curves, a discontinued appliance knob, a hand-carved original. Photogrammetry recovers 3D geometry from overlapping photographs by matching feature points between views, solving for camera poses, and triangulating a dense surface, which produces usable geometry from a phone camera alone. The cost is that the reconstruction is unitless, open (no surface exists where no camera looked), over-tessellated, and of unverified dimensional accuracy, so every scan requires an explicit scale recovery step and a manual repair pass before a slicer will accept it.

## Why the output is unitless and open

Structure-from-motion (SfM) recovers camera poses and a sparse point cloud from correspondences between images. A set of correspondences constrains the scene only up to a **similarity transform**: rotation, translation, and a single global scale are unobservable from image content alone, because doubling the size of the object and doubling every camera distance produces identical images. Multi-view stereo (MVS) then densifies that solution into depth maps and a mesh, inheriting the same ambiguity. Two consequences follow directly, and both drive the cleanup work below.

First, **the mesh has no unit**. Its numeric dimensions are whatever the solver's internal baseline happened to be. Scale must be supplied from outside the photographs, by measuring an object of known dimension that appears in the same reconstruction.

Second, **the mesh is a shell, not a solid, and it is only closed where cameras saw the surface**. A part resting on a table has no reconstructed underside; that region emerges as a ragged boundary loop, not as a flat face. Background geometry that was visible — the table, the room — reconstructs as well, arriving as disconnected islands attached to nothing.

## Capture: the stage that bounds final quality

Reconstruction quality is bounded by the density and reliability of feature matches, so capture decisions dominate everything downstream.

- **Overlap.** Consecutive frames must overlap substantially, orbiting the part in two or three rings at different heights, so that every surface point appears in several views. A few dozen photographs is a working starting point for a small object; deeply occluded geometry needs more.
- **Texture is the signal.** Matte, textured surfaces match well. Shiny, transparent, or uniformly coloured parts fail because **specular highlights are view-dependent**: the bright spot moves across the surface as the camera moves, so the matcher associates image features with the light's reflection rather than with a fixed point on the object. The standard remedies add temporary texture — dusting the part with foot spray or chalk powder, or marking featureless plastic with pencil.
- **Fixed lighting, moving camera.** Diffuse, even illumination (overcast sky or a light tent) keeps a surface point's appearance stable between frames. Move the camera and leave the object still. If a turntable is used instead, the static room behind it becomes a rigid feature set inconsistent with the rotating object, so the background must be featureless.
- **Include a scale reference.** A ruler, a printed checkerboard, or any object of precisely known dimension, placed on the same surface as the part and in view of the same photographs, supplies the missing scale factor.

## The pipeline: Meshroom and the alternatives

[Meshroom](https://github.com/alicevision/Meshroom/releases/tag/v2025.1.0) is the open-source implementation: a node-graph interface over the AliceVision framework. Release 2025.1.0 (August 2025) reworked the node system and added dedicated pipelines, so photographs are dropped in, a photogrammetry pipeline is selected, and the graph runs. The stages are feature extraction, structure-from-motion, depth-map estimation, meshing, and texturing, with an OBJ file as output. **Depth-map estimation requires an NVIDIA GPU with CUDA**; it is the stage that dominates runtime, and runtime scales with the number of input images.

**RealityScan** is Epic's rebrand of RealityCapture; the desktop application became [RealityScan 2.0 in June 2025](https://www.cgchannel.com/2025/06/epic-games-releases-realityscan-2-0-and-realityscan-mobile-1-7/), sharing its name with the mobile capture application RealityScan Mobile, and remains free for individuals and small teams below Epic's revenue threshold. It is faster than Meshroom, and the phone application guides capture, but it is closed-source and the desktop build is Windows-only. [COLMAP](https://github.com/colmap/colmap) is the academic reference SfM/MVS implementation, command-line driven, with accurate camera poses; it is commonly used as the front end for Gaussian-splatting workflows and exposes more parameters with less guidance.

## Cleanup: from shell to solid

The raw mesh carries a few million triangles, boundary loops where no camera looked, disconnected background debris, and arbitrary units. MeshLab and Blender divide the work.

**Delete the junk.** Remove the ground plane and disconnected islands (MeshLab: *Remove Isolated Pieces*). Doing this first prevents background geometry from being carried through the remaining, slower operations.

**Decimate.** Slicing time and memory scale with triangle count, and a scan carries far more triangles than its shape needs. Blender's Decimate modifier, or MeshLab's *Simplification: Quadric Edge Collapse Decimation*, preserves shape well because quadric error metrics collapse edges in the order of least deviation from the original surface. Decimating early makes every later operation cheaper.

**Recover scale.** Measure the reference object inside the mesh (MeshLab's measuring tool, or Blender's `N`-panel dimensions) and apply one uniform factor:

```
scale_factor = real_dimension_mm / measured_dimension_in_mesh
```

In Blender: select all, `S`, type the factor, then *Object → Apply → Scale*. **Applying the scale is not cosmetic** — an unapplied object scale leaves the mesh data in its original units, and modifiers and exporters that read mesh data rather than object transforms will disagree with the viewport. Verify against a *second* known feature: if the ruler reads 100 mm and an independent bolt-hole spacing also reads correctly, the single-factor assumption holds. **No published bound covers the dimensional accuracy of an arbitrary phone-camera reconstruction**; it depends on capture geometry, image count and the reference measurement, and it has to be established per scan with calipers. Any feature with a real fit requirement — a bore, a mating face — should be modelled explicitly rather than trusted from the scan.

**Close and solidify.** MeshLab's *Close Holes* covers small gaps. The missing underside is better handled in Blender: delete the ragged boundary, fill with `F` or grid fill, or boolean the shell against a box to produce a flat printable base. Blender's Solidify modifier adds wall thickness where a shell is to be reprinted as a shell. The target is a watertight, manifold mesh; the companion article on [repairing broken STLs before slicing](/articles/cad-3dprint/2026-08-15-repairing-broken-stl-before-slicing/) covers the verification and repair tooling, all of which applies to scan meshes.

**Hybrid remodelling for mechanical parts.** The scan-as-reference pattern imports the cleaned mesh into FreeCAD or Blender and rebuilds the *functional* surfaces — holes, flats, mounting bosses — as parametric geometry aligned to the scan, retaining the mesh only for organic surfaces. Accuracy at the interfaces then comes from the model rather than from the reconstruction.

## Slicing

Export STL or [3MF](/articles/cad-3dprint/2026-08-11-3mf-vs-stl-3d-printing/) and treat the result as an organic model. Scan-derived parts rarely present flat faces in useful orientations, so [tree supports](/articles/cad-3dprint/2026-08-14-orcaslicer-tree-supports/) are the usual choice, and orientation should place the least accurate reconstructed surface where tolerance does not matter. Print a first article, caliper it against the original, and rescale by the measured error.

## Pitfalls

- **A shiny or transparent part reconstructs as noise or not at all.** Specular highlights and refracted background move between frames, so matched features do not correspond to fixed surface points. Temporary matte powder or pencil marking is the fix.
- **A turntable capture against a textured background yields a smeared or duplicated model.** The static room is rigid relative to the camera while the object rotates, so the solver fits two incompatible rigid motions to one scene.
- **Scaling in Blender without *Apply → Scale* produces a print at the wrong size.** The object transform is scaled while the underlying mesh data is not, and exporters or modifiers reading mesh data see the original numbers.
- **Verifying scale against the same feature used to compute it always succeeds.** The check is circular; a second, independently known dimension is required to detect a mis-measured reference.
- **The slicer reports the model as non-manifold or silently drops geometry.** The reconstruction is an open shell with boundary loops where no camera looked, plus disconnected background islands; both must be removed or closed before export.
- **Decimating after hole filling wastes the fill.** Edge collapse redistributes vertices across the newly created patch, and thin filled regions are the first to degrade.
- **Depth-map estimation stalls or fails without CUDA.** That stage of the AliceVision pipeline requires an NVIDIA GPU.

**Try next:** photograph a broken part 40 times alongside a 123-block or ruler, run it through Meshroom 2025.1, and take it through decimate, scale recovery, solidify, and slice; caliper the print against the original and record where the scan error is visible.
