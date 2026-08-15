---
title: "Dune3D: a constraint-solver parametric CAD that isn't FreeCAD"
date: 2026-08-13
track: cad-3dprint
summary: "Dune3D is a free, history-based parametric computer-aided design application that joins SolveSpace's 3D constraint solver to the OpenCASCADE geometry kernel and Horizon EDA's editor. The group-based workflow, a STEP export, and the boundary where FreeCAD remains necessary."
reading_time: 6
tags: [dune3d, cad, parametric, solvespace, step]
sources:
  - title: "Dune 3D (official site)"
    url: "https://dune3d.org/"
  - title: "dune3d/dune3d (GitHub)"
    url: "https://github.com/dune3d/dune3d"
  - title: "Why another 3D CAD application? — Dune3D docs"
    url: "https://docs.dune3d.org/en/latest/why-another-3d-cad.html"
  - title: "Release v1.4.0 Einstein"
    url: "https://github.com/dune3d/dune3d/releases/tag/v1.4.0"
  - title: "Dune 3D: Open Source 3D Parametric Modeler From The Maker Of Horizon EDA — Hackaday"
    url: "https://hackaday.com/2024/05/05/dune-3d-open-source-3d-parametric-modeler-from-the-maker-of-horizon-eda/"
---

**Gist.** History-based parametric computer-aided design (CAD) breaks when a later feature refers to a face or edge by a name that an earlier edit reassigns — the [topological naming](/articles/cad-3dprint/2026-07-30-freecad-topological-naming) failure that dominates FreeCAD practice. **Dune3D** inverts the layering: a constraint solver holds the relationships between geometric entities, and the geometry kernel is asked only to realise solids from the solved result, so references are carried as constraints rather than as generated names. The cost is scope — no finite-element analysis, no computer-aided manufacturing (CAM), no scripting layer, no assembly workbench — so it models solids and exports them, and nothing else.

Dune3D is written by the author of **Horizon EDA**, an electronic design automation package in the same family of tools as KiCad. It combines **SolveSpace's constraint solver** with the **OpenCASCADE** geometry kernel, presented through Horizon EDA's editor interface. The release referenced here is **v1.4.0**, licensed GPL-3.0, with Linux and Windows builds.

## The model: the group list is the history

There is no feature tree in the FreeCAD sense. A Dune3D document is an **ordered list of groups**, and each group is a single operation consuming the geometry produced by the groups preceding it. The core group types:

| Group | Does |
|---|---|
| Sketch | 2D/3D wireframe on a workplane, constrained |
| Extrude | Push a sketch region into a solid |
| Lathe / Revolve | Spin a profile around an axis |
| Loft | Bridge two sketches |
| Linear / Polar array | Repeat prior groups |
| Fillet / Chamfer | Round or bevel edges |
| Mirror, Clone, Pipe | Duplicate / sweep |

The ordering is the invariant. **A group may reference only groups that precede it**, which makes the document a linear dependency chain with no cycles to detect and no resolution order to compute: re-evaluation runs from the edited group to the end of the list. Editing an early group therefore re-solves everything downstream, which is the ordinary parametric promise. What distinguishes Dune3D is the form those downstream references take.

## Sketching is non-modal and admits 3D constraints

FreeCAD's Sketcher is modal: entering a sketch locks the interaction to one plane until the sketch is closed. Dune3D's sketcher is not. Lines, arcs, and Bezier curves are drawn directly in the 3D viewport on any workplane, and **constraints may be placed in 3D, between entities belonging to different groups**.

That last property is the load-bearing one. A conventional history modeller records a later feature's attachment as a reference to a named boundary-representation element — `Face6`, `Edge12` — and those names are assigned by the kernel each time the model is rebuilt. Change an earlier feature so that the kernel enumerates faces differently, and `Face6` now denotes some other face, or none; the later feature attaches to the wrong geometry or fails outright. In Dune3D the relationship is instead expressed as a constraint on the geometric entity itself, and **the solver carries that relationship across the re-solve** rather than re-resolving a name. This is a structural avoidance of the topological-naming class of breakage, not a heuristic that repairs names after the fact.

The solver's own contract is the familiar one for constraint-based sketching: entity coordinates are unknowns, constraints are equations relating them, and the sketch reaches the **fully-constrained state when the remaining degrees of freedom reach zero** — which the editor signals on the affected entities. A sketch left with degrees of freedom is not an error; it is an under-determined system whose free entities the solver is at liberty to move when anything nearby changes.

A minimal workflow — a constrained plate with a hole, exported as STEP (Standard for the Exchange of Product model data):

```
1. New document. A default workplane (XY) exists.
2. Add group -> Sketch. Draw a rectangle.
   - Constrain: two "distance" constraints -> width 60, height 40.
   - Anchor one corner to the origin (coincident constraint).
   - Add a circle; "diameter" constraint = 8; two distances locate its centre.
   The sketch reports fully constrained when DOF = 0.
3. Add group -> Extrude. Select the rectangle region, drag/enter 5 mm.
   The circle region becomes a through-hole (subtracted).
4. Add group -> Chamfer. Pick the top edges, 0.8 mm.
5. File -> Export -> STEP.  Fillets/chamfers survive as real BREP.
```

Two details in that sequence carry weight. The circle becomes a **through-hole by region subtraction at extrude time** — the hole is not a separate feature applied afterwards, so it cannot become detached from the sketch that defines it. And the STEP export carries **boundary representation (BREP)** geometry, meaning fillets and chamfers leave the tool as analytic surfaces rather than as a triangulated approximation; a mesh export such as STL would discard that.

Every dimension in the sequence is a constraint that can be double-clicked and retyped, after which the model re-solves in place. There is no separate parameter-spreadsheet abstraction of the kind [FreeCAD provides](/articles/cad-3dprint/2026-07-30-freecad-spreadsheet-parametric); the dimension values are the constraints. Changing the width constraint from 60 to 90 re-solves the hole position, the chamfer, and the exported solid, because each of those was expressed relative to constrained entities rather than to named faces.

## Install

Flathub is the lowest-friction path on Linux:

```bash
flatpak install flathub org.dune3d.dune3d
flatpak run org.dune3d.dune3d
```

Windows builds are published as an installer on the GitHub releases page. Building from source is a Meson job (`meson setup build && ninja -C build`); the dependencies are OpenCASCADE, GTK4, and glm.

## Where the difference is architectural

The project's own "why another 3D CAD application?" document names three FreeCAD frustrations Dune3D responds to directly: a **modal, 2D-only sketcher**, **no constraints for 3D geometry**, and **fragile referencing**. Three consequences follow.

- **Solver-first rather than kernel-first.** SolveSpace determines geometry from constraints; OpenCASCADE realises the resulting solids. FreeCAD layers these the other way round, which is where naming fragility enters — the identity of a reference is produced by the kernel rather than held by the solver.
- **One editing paradigm.** Sketching and 3D constraint placement use the same interaction, inherited from Horizon EDA. There is no workbench to switch between.
- **Deliberately narrow scope.** No finite-element analysis, no CAM, no Python scripting layer, no assembly workbench. The tool models solids and exports them, STEP and STL among the formats.

That scope is the honest caveat rather than a footnote. A workflow requiring [Path/CAM](/articles/cad-3dprint/2026-08-04-freecad-path-cam-workbench), [TechDraw 2D drawings](/articles/cad-3dprint/2026-07-30-freecad-techdraw-2d-drawings), or a scripting application programming interface (API) is not served by Dune3D at v1.4.0; it is a solid modeller, not a suite. For the narrower job of drawing a part and producing a clean STEP for a slicer or a fabrication quote, the constraint-driven flow removes the failure mode that makes the third edit of a FreeCAD model expensive.

No published benchmark compares edit-propagation times or re-solve robustness between the two tools, so the claim above is architectural, not measured.

## Pitfalls

- **A sketch that never turns green is under-constrained, not merely untidy.** With non-zero degrees of freedom the solver may reposition free entities during a downstream re-solve, so a dimension that appeared correct on screen shifts after an unrelated edit.
- **Group order constrains what can be referenced.** A group cannot reference geometry from a group later in the list, so a constraint that seems geometrically obvious is unavailable until the referenced group is moved earlier — and moving it changes what it consumes.
- **Exporting STL discards the analytic geometry.** Fillets and chamfers survive a STEP export as BREP surfaces; the mesh export replaces them with facets, and a downstream tool cannot recover the original radius from that.
- **Scope gaps are absent features, not missing configuration.** There is no scripting API to automate a repetitive edit and no assembly workbench to hold multiple parts in relation, so those workflows have no in-application workaround.
- **Regions, not curves, drive extrusion.** An extrude consumes a closed sketch region; a profile with an unclosed gap between endpoints yields no region, and the extrude group produces nothing rather than reporting a broken curve.
