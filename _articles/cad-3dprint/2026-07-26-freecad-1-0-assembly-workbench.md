---
title: "FreeCAD 1.0's Assembly Workbench: Joints, Solver, Toponaming"
date: 2026-07-26
track: cad-3dprint
summary: "FreeCAD 1.0 shipped a built-in Assembly workbench with the Ondsel solver and a topological-naming mitigation, making multi-part joint constraints a native workflow."
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

**Gist.** Before version 1.0, FreeCAD had no first-class representation of kinematic constraints between independent parts: multi-part models were positioned by hand or by third-party add-ons such as A2plus and Assembly3. FreeCAD **1.0, released 19 November 2024**, added a core **Assembly workbench** whose joints are solved by the **Ondsel solver**, together with Realthunder's **topological-naming mitigation**, described in the release announcement as reducing — not removing — the breakage caused by topology changes in a parametric chain. The cost is a second constraint system layered on top of the sketcher's: every joint holds references into part geometry, so an assembly is only as stable as the names of the faces and edges it points at, and the mitigation reduces rather than eliminates the cases where those references break.

## The two changes and how they interact

The Assembly workbench supplies the same conceptual service as SolidWorks' Mates or Fusion 360's Joints: given a set of declared constraints between parts, compute part placements that satisfy all of them simultaneously. The solver contributed by Ondsel performs that computation.

The topological-naming problem is the reason the first change was not sufficient on its own. **A joint does not store "the top face"; it stores a reference to a named subelement such as `Face6` on a specific object.** Names are assigned when a solid's boundary representation is regenerated. Editing an early feature in a part's history regenerates that boundary, and the face that was `Face6` may become `Face7` or cease to exist. The consequence before 1.0 was silent: **later features in the same part, and joints in an assembly pointing at those faces, could rebind to different geometry or detach entirely**, and the repair was to recreate the constraint by hand. The mitigation makes the chain "add a sketch feature → reference the resulting face in a joint → keep editing upstream" survive recompute in more cases than before. It is documented as a mitigation rather than a fix, and cases that still break are expected.

## The assembly model

An Assembly is a container object in the document tree. Parts enter it three ways:

- **Insert Component** — brings an existing Part or Body in as a linked instance.
- Creating a new Part or Body from the Assembly menu, which places it inside the assembly.
- Dragging an existing object onto the Assembly node in the tree.

**Every inserted part starts with all six degrees of freedom (DOF) free** — three translations and three rotations. An assembly acquires meaning only once a fixed frame exists and joints remove DOF relative to it.

### Grounding is the invariant

*Toggle Grounded* locks a part's position and orientation to the assembly origin by creating a `GroundedJoint`. **At least one grounded part is required**: without it the constraint system has no fixed frame, and any solution can be translated or rotated arbitrarily and remain a solution. The symptom of a missing ground is not an error dialog but an assembly whose parts hold their relative positions correctly while the whole cluster drifts when dragged.

### Joints as DOF arithmetic

A joint is declared by selecting reference geometry — faces, edges, or local coordinate systems — on each of two parts. Each joint type removes a fixed number of the six relative DOF:

| Joint | Degrees of freedom | Typical use |
|---|---|---|
| Fixed | Removes all 6 | Bolted panels, glued shells |
| Revolute | Leaves rotation about one axis | Hinges, pivots, wheels on an axle |
| Slider | Leaves translation along one axis | Drawer rails, linear guides |
| Cylindrical | Leaves rotation and translation on one axis | A pin free to spin and slide in a bore |
| Ball | Leaves free rotation about a point, no translation | Ball-and-socket linkages |
| Distance | Holds a fixed separation between references | Standoffs, spacers |
| Parallel / Perpendicular / Angle | Constrains relative axis orientation only | Aligning brackets, angled supports |
| Rack and Pinion / Screw / Gears / Belt | Couples the motion of two other joints | Lead screws, gear trains |

The last group is different in kind from the rest: **those joints constrain other joints rather than geometry directly**, so they presuppose that the revolute or slider joints they couple already exist.

With the joints declared, dragging a part in the 3D view moves every kinematically connected part consistently — the interactive counterpart of solving the constraint system, and the cheapest available check that a mechanism has the intended range of motion rather than merely the intended static pose.

## Worked case: a lid on an enclosure

Two parts, an enclosure base and a lid seating flush on its wall, joined rigidly:

1. **Model the parts separately** as ordinary Parts or Bodies, in one document or in separate documents merged by linking.
2. **Create the assembly.** Assembly workbench → *Create Assembly* adds an `Assembly` container to the tree.
3. **Insert both parts** with *Insert Component*, or by dragging them onto the `Assembly` node.
4. **Ground the base.** Select `Base`, click *Toggle Grounded*; the lock icon marks the fixed frame.
5. **Select mating references** — the top face of the base wall and the corresponding bottom face of the lid — using Ctrl-click to multi-select across the two parts.
6. **Create the joint.** *Create Fixed Joint* removes all six DOF. For a hinged lid, select an edge on each part and use *Create Revolute Joint* instead, anchored where the hinge pin sits; the remaining rotational DOF is then draggable.
7. **Verify by dragging.** The solver places `Lid` relative to `Base`. Dragging `Base` should carry `Lid` with it; if it does not, the joint references did not bind to the intended geometry.
8. **Edit upstream and recompute.** Adding a boss to the base sketch and recomputing exercises the toponaming mitigation directly: the joint references are expected to survive rather than detach.

Step 7 is the one worth repeating after every upstream edit. **A joint that has rebound to the wrong face still solves** — the system is satisfiable, only wrong — so a silent failure is visible as a misplacement, not as a diagnostic.

## Scripting

Assemblies remain ordinary FreeCAD documents, so the Python console reaches them. The assembly is reachable through `App.ActiveDocument` by its internal name, and its `Group` holds the inserted parts. Joints are document objects in their own right, carrying a joint type and references to the two connected geometries, so they can be listed and their properties read from the console like any other object. **The joint application programming interface (API) is new in 1.0 and its documentation is thin**, which makes it substantially rougher than sketch scripting. For parts already generated parametrically, driving joint placement from the same parameter set — recomputing a hinge offset when a panel height changes — keeps the assembly consistent with the parts without manual redragging.

## Pitfalls

- **No grounded part.** The assembly solves, but the whole cluster translates and rotates freely when any member is dragged: the constraint system is under-determined by six DOF because no fixed frame was declared.
- **A joint that rebound to the wrong face.** After an upstream edit the model recomputes without error and a part sits in the wrong place; the named subelement the joint referenced was renumbered by boundary-representation regeneration, and the mitigation did not cover that case.
- **Coupling joints declared first.** Rack and pinion, screw, gear and belt joints constrain two existing joints, so declaring one before the revolute or slider joints it couples leaves nothing to couple.
- **Over-constraining with redundant joints.** Adding a Parallel joint between axes already made parallel by a Revolute joint states the same condition twice; the redundancy is a property of the declared system, not of the geometry, and appears only when the solver reports failure.
- **Assuming Distance is a fixed joint.** A Distance joint holds separation between two references and leaves the remaining orientation DOF free, so parts held at the correct spacing may still rotate relative to each other.
- **Treating a drag test as a collision check.** Dragging through a revolute range demonstrates the kinematics of the joint, not that the swept volumes of the parts stay disjoint.
