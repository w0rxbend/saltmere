---
title: "Dune3D: a constraint-solver parametric CAD that isn't FreeCAD"
date: 2026-08-13
track: cad-3dprint
summary: "Dune3D is a free, history-based parametric CAD app that glues SolveSpace's 3D constraint solver to the OpenCASCADE kernel and Horizon EDA's editor. Here's the group-based workflow, a STEP export, and an honest look at where it beats FreeCAD."
reading_time: 5
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

Most open-source parametric CAD is FreeCAD, and FreeCAD's pain points are well documented on this journal — chiefly [topological naming](/articles/cad-3dprint/2026-07-30-freecad-topological-naming), where an edit renames the faces your later features referenced and the model detonates. **Dune3D** takes a different bet. Written by **Lukas Wallmann** (the author of Horizon EDA, the KiCad-adjacent PCB tool), it wires **SolveSpace's constraint solver** to the **OpenCASCADE** geometry kernel behind Horizon EDA's editor UI. Current release: **v1.4.0 "Einstein"** (January 2026), GPL-3.0, Linux and Windows.

## The model: groups are the history

There is no feature tree in the FreeCAD sense. A Dune3D document is an ordered list of **groups**, and each group is one operation that consumes the geometry of the groups before it. The core types:

| Group | Does |
|---|---|
| Sketch | 2D/3D wireframe on a workplane, constrained |
| Extrude | Push a sketch region into a solid |
| Lathe / Revolve | Spin a profile around an axis |
| Loft | Bridge two sketches |
| Linear / Polar array | Repeat prior groups |
| Fillet / Chamfer | Round or bevel edges |
| Mirror, Clone, Pipe | Duplicate / sweep |

Because the history *is* the group list, editing an early group re-solves everything downstream — the ordinary parametric promise. The interesting part is *how* references survive that re-solve.

## Sketching is non-modal and 3D

FreeCAD's Sketcher is modal: you enter a 2D sketch, you're locked to that plane, you leave. Dune3D's sketcher is neither. You draw lines, arcs, and Bezier curves directly in the 3D viewport, on any workplane, and — crucially — you can place **constraints in 3D**, between geometry that lives in different groups. Instead of naming a face like `Face6` and hoping it keeps that name, you constrain *to the actual geometric entity*; the solver carries that relationship. This is the structural reason Dune3D sidesteps the topological-naming class of breakage rather than papering over it.

A minimal, real workflow — a constrained plate with a hole, exported as STEP:

```
1. New document. A default workplane (XY) exists.
2. Add group -> Sketch. Draw a rectangle.
   - Constrain: two "distance" constraints -> width 60, height 40.
   - Anchor one corner to the origin (coincident constraint).
   - Add a circle; "diameter" constraint = 8; two distances locate its centre.
   The sketch turns fully-constrained green when DOF = 0.
3. Add group -> Extrude. Select the rectangle region, drag/enter 5 mm.
   The circle region becomes a through-hole (subtracted).
4. Add group -> Chamfer. Pick the top edges, 0.8 mm.
5. File -> Export -> STEP (AP214).  Fillets/chamfers survive as real BREP.
```

Every dimension above is a constraint you can double-click and retype; the model re-solves in place. There's no "spreadsheet of parameters" abstraction like [FreeCAD's](/articles/cad-3dprint/2026-07-30-freecad-spreadsheet-parametric) — the numbers *are* the constraints.

## Install

Flathub is the path of least resistance on Linux:

```bash
flatpak install flathub org.dune3d.dune3d
flatpak run org.dune3d.dune3d
```

Windows users grab the installer from the GitHub releases page. Building from source is a straightforward Meson job (`meson setup build && ninja -C build`) if you want bleeding-edge; the deps are OpenCASCADE, GTK4, and glm.

## How it actually differs from FreeCAD

Wallmann's own "why another 3D CAD" writeup names three FreeCAD frustrations Dune3D is a direct answer to: a **modal 2D-only sketcher**, **no constraints for 3D geometry**, and **fragile referencing**. The upshot:

- **Solver-first, not kernel-first.** SolveSpace decides geometry from constraints; OpenCASCADE just realises the solids. FreeCAD is the reverse, which is where naming fragility creeps in.
- **One editing paradigm.** Sketching and assembly-style 3D constraints use the same interaction, borrowed from Horizon EDA. No workbench switching.
- **Deliberately small.** No FEM, no CAM, no Python scripting layer, no assembly workbench. It models solids and exports STEP/STL/DXF. That's the whole scope.

That scope is the honest caveat. If you need [Path/CAM](/articles/cad-3dprint/2026-08-04-freecad-path-cam-workbench), [TechDraw 2D drawings](/articles/cad-3dprint/2026-07-30-freecad-techdraw-2d-drawings), or a scripting API, Dune3D won't replace FreeCAD — it's a focused solid modeller, not a suite. But for the daily job of *drawing a part and getting a clean STEP into your slicer or a fab quote*, the constraint-driven flow is faster and markedly less likely to blow up on the third edit.

**Try next:** `flatpak install flathub org.dune3d.dune3d`, model the plate above, then go back and change the width constraint from 60 to 90 and watch the hole, chamfer, and STEP-ready solid re-solve without a single broken reference — the thing FreeCAD makes you fight for.
