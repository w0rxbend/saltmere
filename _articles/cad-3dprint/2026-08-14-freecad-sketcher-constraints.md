---
title: "FreeCAD Sketcher: Fully Constraining a Sketch and Reading the Degrees of Freedom"
date: 2026-08-14
track: cad-3dprint
summary: "An under-constrained sketch is a set of suggestions rather than a definition: how the degrees-of-freedom counter is computed, how geometric and dimensional constraints differ, and how a bracket profile reaches DoF = 0 without redundancy or conflict."
reading_time: 6
tags: [freecad, sketcher, constraints, degrees-of-freedom, parametric, cad]
sources:
  - title: "FreeCAD Wiki: Sketcher Workbench"
    url: "https://wiki.freecad.org/Sketcher_Workbench"
  - title: "FreeCAD Documentation: Sketcher Workbench (constraints and DoF)"
    url: "https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Sketcher_Workbench.md"
  - title: "FreeCAD Documentation: Sketcher requirement for a sketch"
    url: "https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Sketcher_requirement_for_a_sketch.md"
  - title: "FreeCAD Version 1.1 released — FreeCAD News"
    url: "https://blog.freecad.org/2026/03/25/freecad-version-1-1-released/"
---

**Gist.** Every solid in FreeCAD's Part Design workflow begins as a 2D sketch, and freshly drawn sketch geometry admits infinitely many positions and sizes that satisfy the drawing as entered. The Sketcher's constraint solver removes that freedom one **degree of freedom (DoF)** at a time until exactly one configuration remains, and reports the remaining count in the Solver messages panel. The cost is that the constraint set must be *exactly* determining: too few constraints leave geometry free to drift when an upstream parameter changes, and one too many produces a redundant or conflicting sketch that the solver reports as an error rather than silently ignoring. The behaviour described here is current in FreeCAD 1.1, released 25 March 2026.

## What the degrees-of-freedom counter measures

A DoF is one independent parameter of the sketch's geometry that no constraint has yet fixed. A single line segment drawn on an empty sketch carries **four** DoF: its two endpoints each have an x and a y coordinate, which is equivalent to saying the segment can translate along two axes, rotate, and change length. **The counter is a property of the whole sketch, not of any one edge** — it is the number of free parameters across all geometry minus the number of independent equations the constraints impose.

Each constraint removes one or more DoF, provided it is independent of the constraints already present. A **coincident** constraint between two endpoints identifies two coordinate pairs and removes two. A **horizontal** constraint on a line forces the two endpoint y-coordinates equal and removes one. A **distance** constraint removes one. Dependence, not the constraint's type, decides whether the count falls: a constraint whose equation is already implied by the existing set removes nothing, and FreeCAD classifies it as redundant.

The Sketcher reports state in three visible channels:

- The **Solver messages** panel in the task dialog reports the sketch as under-constrained together with the number of remaining degrees of freedom while that number is greater than zero, and as **fully constrained** when it reaches zero.
- Geometry that still has freedom is drawn **white**; a fully constrained sketch turns **green** throughout.
- Errors — redundant, conflicting, or over-constrained — are reported in the same panel with links that select the offending constraints.

**Reaching zero is a correctness property, not a formality.** FreeCAD does not refuse to pad an under-constrained sketch; it will extrude a white profile without complaint. The consequence appears later. The solver only guarantees that the final geometry satisfies the stated constraints, so any parameter left free may settle on a different value the next time the sketch is solved — after a spreadsheet cell changes, after an upstream feature moves the attachment, or after a vertex is dragged. **An under-constrained sketch therefore has no stable definition of shape across edits, only a shape that happened to be reached on the last solve.**

## The two constraint families

Constraints divide into those that state a relationship and those that state a number, and a durable sketch uses both.

**Geometric constraints** encode relationships without any dimension: **coincident** (joins two points, including making a point concentric with a circle's centre), **horizontal** and **vertical** (aligns a line or a point pair with a sketch axis), **parallel**, **perpendicular**, **tangent**, **equal** (two edges share length or radius), **symmetric** (two points mirror about a line or axis), and **point-on-object** (a point lies on an edge or its extension). These carry design intent through later edits: an **equal** constraint between two holes keeps them equal whatever their radius becomes, whereas two independent radius dimensions do not.

**Dimensional constraints** supply the numbers: **distance** (length of an edge or separation of two points), **horizontal distance** and **vertical distance** (the axis-aligned components), **angle**, and **radius** or **diameter**. Each fixes one scalar and removes one DoF when independent.

The ordering that minimises redundancy is to state geometric relationships first and then add only the dimensions still needed to fix scale and position — every relationship expressed geometrically is one fewer number that a later edit can contradict.

## Driving a bracket profile to zero

Consider an L-shaped bracket outline: six line segments forming a closed loop, drawn roughly with the polyline tool.

1. **Close the loop.** Every corner must be a single coincident point. **A corner with a hairline gap leaves both endpoints free rather than joined**, so the sketch does not reach zero DoF and Part Design rejects the profile as an open wire. Drawing with snapping enabled produces the coincident constraints automatically; otherwise each vertex needs an explicit **coincident**. Six segments start at 24 DoF (four per segment) and six coincidences remove two each, leaving twelve.
2. **Anchor to the origin.** A **coincident** constraint between one corner point and the sketch origin removes the profile's two translational DoF. Nothing in the sketch can float thereafter.
3. **Fix the orientation.** **Horizontal** on the bottom edge and **vertical** on the left edge remove rotation and align the profile to the axes; the remaining edges take **horizontal**, **vertical**, or **perpendicular** against their neighbours. Each independent one of these removes a further DoF.
4. **Dimension the bounding box.** A **horizontal distance** for overall width and a **vertical distance** for overall height fix the outer extent.
5. **Dimension the notch.** Two further distances fix the inner corner of the L — one leg thickness in each direction.
6. **Read the panel.** The message should now read `Fully constrained` and the outline should be green throughout. If it still reports one or more degrees of freedom, a dimension or an alignment is missing; **dragging a vertex reveals which parameter is still free, because only unconstrained parameters move under the drag.**

The useful discipline is to watch the counter after every constraint and check that it fell by the expected amount. A click that leaves the count unchanged has added a dependent constraint, and that is the moment to undo it — not after twenty more clicks, when the redundancy is buried.

## Redundancy, conflict and over-constraint

Adding constraints past zero is an error state rather than extra safety, and FreeCAD distinguishes the cases.

A **redundant constraint** is one whose equation is already implied by the others: a length dimension on an edge that an **equal** plus **symmetric** pair has already determined, for example. The stated relationship is true, so the geometry is not wrong, but the solver reports the redundancy and the sketch now has two places where one number lives. The Sketcher provides a *Select redundant constraints* action that highlights them for deletion.

A **conflicting** sketch results when two constraints demand incompatible values of the same parameter — a line constrained both **horizontal** and to an angle of 30°. There is no solution, the solver reports the conflict, and the correct response is to undo the last constraint rather than to fight the solver by deleting geometry.

The distinction matters because the two have different fixes: redundancy is resolved by deleting the *later, weaker* statement of an already-known fact, whereas conflict means one of the two constraints expresses the wrong intent and must be replaced, not merely removed.

## Pitfalls

- **A padded white sketch produces geometry that changes shape on the next solve.** FreeCAD extrudes an under-constrained profile without warning, so the defect surfaces only when an upstream parameter changes and the free parameters resolve differently.
- **A hairline gap at a corner leaves the loop open.** The profile appears closed on screen at normal zoom, but the sketch never reaches zero DoF and Part Design rejects it as an open wire.
- **A constraint that leaves the DoF counter unchanged has been absorbed as redundant.** The sketch still looks correct; the duplicate statement of the same fact only becomes visible as a solver message later, after many more constraints hide its origin.
- **Dimensioning a length that `equal` plus `symmetric` already implied triggers a redundancy report**, because the symmetry and equality together have already determined that scalar.
- **Constraining a line both horizontal and to a non-zero angle yields a conflicting sketch with no solution**; deleting geometry does not fix it, since the incompatible pair of constraints is the cause.
- **Fixing scale before fixing relationships multiplies the dimensions needed.** Every relationship left unexpressed geometrically becomes an independent number that a later parameter change can contradict.
