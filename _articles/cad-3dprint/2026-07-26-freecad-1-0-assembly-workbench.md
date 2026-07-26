---
title: "FreeCAD 1.0's Assembly Workbench: Parts That Actually Move Together"
date: 2026-07-26
track: cad-3dprint
summary: "FreeCAD 1.0 shipped a real, built-in Assembly workbench with a solver and a toponaming fix — for the first time you can join multiple parts with joints and trust the model to hold."
reading_time: 5
tags: [freecad, assembly, cad, joints, solver, 3d-printing, parametric]
sources:
  - title: "FreeCAD Version 1.0 Released"
    url: "https://blog.freecad.org/2024/11/19/freecad-version-1-0-released/"
  - title: "FreeCAD-documentation: Assembly Workbench"
    url: "https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Assembly_Workbench.md"
  - title: "Tutorial: Getting Started with the Assembly Workbench"
    url: "https://blog.freecad.org/2024/09/30/tutorial-getting-started-with-the-assembly-workbench/"
  - title: "Ondsel: integrated assembly added to the FreeCAD core"
    url: "https://www.ondsel.com/blog/assembly-workbench-preview/"
---

FreeCAD 1.0 landed on **November 19, 2024**, and the release notes are unusually specific about why it finally earned that version number: two long-standing gaps got closed at once — a working **Assembly workbench** built into the core, and a fix for the notorious **toponaming problem**. Both had been "the" missing feature for over a decade of 0.x releases. Neither was a quick patch.

## What actually changed in 1.0

Before 1.0, FreeCAD had no first-class way to assemble independent parts with real kinematic constraints. You could fake it with the Part workbench and manual placement, or reach for a third-party add-on (A2plus, Assembly3), but nothing shipped in the box that understood joints as joints. The 1.0 announcement calls out the new Assembly workbench as using "the brand-new Ondsel solver" — a solver contributed by Ondsel (a company built around FreeCAD) that computes valid part positions from a set of joint constraints, the same conceptual job SolidWorks' Mates or Fusion 360's Joints do.

The second headline item is the toponaming fix: FreeCAD incorporated Realthunder's topological-naming mitigation algorithm, which the release notes describe as adding "resiliency against topology changes in a parametric chain." This matters directly for assemblies — before the fix, editing an early feature in a part's history could silently break references (faces, edges) used later in the same part *or* in joints that pointed at those faces, forcing you to recreate constraints. The fix doesn't eliminate every edge case, but it's described as a solid, working improvement rather than a stopgap, and it's what makes chaining "insert a sketch feature → reference it in a joint → keep editing upstream" trustworthy enough to build real assemblies on.

Put together: 1.0 is the release where FreeCAD stopped being "great single-part parametric CAD with fragile assemblies bolted on" and became a tool where multi-part, constrained assemblies are a native, reasonably durable workflow.

## The assembly model

An Assembly in FreeCAD is a container object. You populate it two ways:

- **Insert Component** — pulls an existing Part (or Body) into the active assembly as a linked instance.
- **Insert a New Part** — creates a fresh Part directly inside the assembly.

You can also drag parts from elsewhere in the tree into the Assembly object. Every part you insert is initially free to move in space; the assembly only becomes meaningful once you **ground** something and start adding **joints**.

Grounding uses the "Toggle Grounded" tool, which locks a part's position and orientation to the assembly's origin by creating a `GroundedJoint`. Every assembly needs at least one grounded part — it's the anchor everything else is measured against. Skip this step and the solver has no fixed frame of reference to solve relative to.

Joints then describe the allowed relative motion between two parts, expressed by selecting reference geometry (faces, edges, or local coordinate systems) on each part:

| Joint | Degrees of freedom removed | Typical use |
|---|---|---|
| Fixed | All 6 (locks parts together rigidly) | Bolted panels, glued shells |
| Revolute | Allows rotation about one axis only | Hinges, pivots, wheels on an axle |
| Slider | Allows translation along one axis only | Drawer rails, linear guides |
| Cylindrical | Rotation + translation on the same axis | A pin free to spin and slide in a bore |
| Ball | Free rotation about a point, no translation | Ball-and-socket linkages |
| Distance | Holds a fixed separation between references | Standoffs, spacer constraints |
| Parallel / Perpendicular / Angle | Constrains relative axis orientation only | Aligning brackets, angled supports |
| Rack and Pinion / Screw / Gears / Belt | Couples motion of two other joints | Mechanisms — lead screws, gear trains |

Once joints are defined, the Ondsel solver resolves the whole system: drag a part in the 3D view and every other part that's kinematically connected to it moves consistently with its joints, the same way dragging one link of a real hinge moves everything attached to it.

## Workflow: bolting a lid onto an enclosure

A concrete two-part case — an enclosure base and a lid that should sit flush and be free to lift off (a fixed joint, since a lid usually doesn't hinge) — looks like this:

1. **Model the parts separately.** Build `Base` and `Lid` as ordinary Parts (or Bodies), each with its own sketches/features. Keep them in the same document, or as separate documents merged via linking.
2. **Create the assembly.** Assembly workbench → *Create Assembly*. This adds an `Assembly` container to the tree.
3. **Insert both parts.** Use *Insert Component*, or drag `Base` and `Lid` into the `Assembly` node.
4. **Ground the base.** Select `Base`, click *Toggle Grounded*. You'll see the lock icon appear — this is now the fixed reference frame.
5. **Pick mating references.** Select the top face of `Base`'s wall (where the lid seats) and the corresponding bottom face of `Lid`. Ctrl-click to multi-select across the two parts.
6. **Create the joint.** With both faces selected, choose *Create Fixed Joint* (or, if the lid should hinge open, select an edge on each part instead and use *Create Revolute Joint*).
7. **Solve and check.** The solver snaps `Lid` into place relative to `Base`. Drag `Base` in the 3D view — if `Lid` moves with it, the joint is doing its job.
8. **Iterate on the parts, not just the assembly.** Because of the toponaming fix, you can go back and, say, add a boss to `Base`'s sketch, recompute, and the joint references should survive rather than silently detaching.

For a hinged lid instead of a lifted one, swap step 6 for a Revolute Joint anchored on the edge where the hinge pin would sit, and the solver gives you an actual swing range you can drag through — a legitimate mechanism check, not just a static snapshot.

## Scripting the assembly

Assemblies are still FreeCAD documents, so the Python console reaches them too. `App.ActiveDocument.Assembly` gives you the assembly object; its `Group` holds the inserted parts, and joints are objects in their own right (with a `Type` and references to the two connected geometries) that you can create or inspect with `App.ActiveDocument.addObject`. It's rougher around the edges than sketch scripting — the joint API is new in 1.0 and documentation is thin — but if you're already generating parts parametrically, driving joint placement from the same parameter set (e.g., recomputing a hinge offset when a panel height changes) is the natural next step rather than manually redragging things in the GUI.

**Try next:** build the enclosure-and-lid assembly above, then add a third part — a latch — connected to the lid with a Revolute Joint, and drag it through its range to confirm it doesn't collide with the base.
