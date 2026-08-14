---
title: "FreeCAD Sketcher: Fully Constraining a Sketch and Reading the Degrees of Freedom"
date: 2026-08-14
track: cad-3dprint
summary: "A sketch that isn't fully constrained is a set of suggestions, not a definition — here's how to read the degrees-of-freedom counter, pick geometric vs dimensional constraints, and drive a bracket profile to green (DoF = 0) without over-constraining it."
reading_time: 5
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

Every solid model in FreeCAD's Part Design workflow starts as a 2D sketch, and every sketch starts unconstrained — a bag of lines that will slide, stretch, and rotate the moment you touch them. The Sketcher's job is to pin that geometry down until exactly one shape satisfies your rules. FreeCAD tracks how much freedom remains with a **degrees of freedom** counter, and the discipline of driving it to zero is what separates a model you can edit six months later from one that explodes when you change a single number. This is current in FreeCAD 1.1 (released March 25, 2026), but the concepts are unchanged since 1.0.

## What degrees of freedom actually count

A single line segment, freshly drawn, has **four** degrees of freedom: it can move horizontally, move vertically, stretch in length, and rotate. Each constraint you add removes one or more of those DoF. When the count reaches zero, the geometry is *fully constrained* — there is exactly one valid position and size for every element. FreeCAD shows the running total in the **Solver messages** panel of the Sketcher task dialog: unconstrained geometry appears white, and the panel reads `Under-constrained: N degrees of freedom`. When you close the last gap, the message flips to `Fully constrained` and every edge turns **green**.

Green is the goal. Not because FreeCAD refuses to extrude a loose sketch — it will happily pad a white one — but because an under-constrained sketch has undefined behavior under edits. Change a pad length or a spreadsheet parameter upstream, and any element that wasn't locked down is free to drift to a new solution the solver finds equally valid.

## Two families of constraints

Constraints come in two kinds, and a robust sketch uses both.

**Geometric constraints** define *relationships* without numbers: **coincident** (join two endpoints, or make a point concentric), **horizontal/vertical** (force a line or point-pair onto an axis), **parallel**, **perpendicular**, **tangent**, **equal** (two edges share length or radius), **symmetric** (two points mirror across a line or axis), and **point-on-object**. These are cheap DoF removers and they encode design intent — "these two holes are always the same size" survives every later edit.

**Dimensional constraints** supply the *numbers*: **distance** (length of a line or gap between points), **horizontal/vertical distance**, **angle**, and **radius/diameter**. Reach for geometric constraints first to express intent, then add the minimum dimensions needed to fix scale.

## Step-by-step: constraining a bracket profile to 0 DoF

Take a simple L-shaped bracket outline — six line segments forming a closed loop, drawn roughly with the polyline tool.

1. **Close and coincide.** Make sure every corner is a single coincident point (endpoints snapped together). The solver won't fully constrain a loop with a hairline gap. Draw with snapping on, or add **coincident** constraints at each vertex. Six clean corners drop you from ~24 DoF to around 8.
2. **Anchor to the origin.** Select the bottom-left corner point and the sketch origin, apply **coincident**. This kills the two translation DoF for the whole profile — nothing floats anymore.
3. **Square it up.** Select the bottom edge, apply **horizontal**; select the left edge, apply **vertical**. Add **vertical**/**horizontal** to the remaining edges (or **perpendicular** between adjacent ones). This removes rotation and forces the right-angle geometry. The counter should now read a small single-digit number.
4. **Dimension the outer box.** Add a **horizontal distance** for overall width (say 60 mm) and a **vertical distance** for overall height (40 mm).
5. **Dimension the notch.** Add the two distances that define the inner corner of the L — the leg thickness one way (12 mm) and the other (15 mm).
6. **Read the panel.** It should now say `Fully constrained` and the whole outline turns green. If it still shows `1 degree of freedom`, one dimension or a horizontal/vertical is missing — grab a green-less edge and drag it to see what still moves.

## Avoiding over-constraint and redundancy

Adding more constraints past zero isn't safer — it's an error. If you dimension a length that a symmetric-plus-equal pair already implied, the solver flags a **redundant constraint** (the relationship is true but stated twice; use *Select redundant constraints* to find it and delete it). If two constraints demand incompatible things — a line both horizontal and at 30° — you get an **over-constrained**, *conflicting* sketch, and FreeCAD warns you to undo. The habit that avoids both: constrain incrementally, watch the DoF counter drop by exactly what you expect after each click, and stop the instant it hits zero.

**Try next:** open any past sketch that shows white edges, drag a vertex to see what floats, then add constraints one at a time — watching the Solver messages count fall — until it reads `Fully constrained`.
