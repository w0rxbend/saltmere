---
title: "From parametric body to G-code: the FreeCAD CAM workbench"
date: 2026-08-04
track: cad-3dprint
summary: "The CAM workbench, renamed from Path in FreeCAD 1.0, converts a parametric solid into toolpaths and a grbl or LinuxCNC .nc file, with Job, Stock, Tool Controller and Operation objects as the intermediate state."
reading_time: 6
tags: [freecad, cam, cnc, gcode, milling]
sources:
  - title: "FreeCAD CAM Workbench documentation"
    url: "https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/CAM_Workbench.md"
  - title: "The Path workbench in FreeCAD is going away (rename to CAM)"
    url: "https://www.ondsel.com/blog/path-wb-going-away/"
  - title: "FreeCAD 1.1.3 released"
    url: "https://blog.freecad.org/2026/07/25/freecad-1-1-3-released/"
  - title: "FreeCAD CNC: CAD and CAM workflow"
    url: "https://wayofwood.com/freecad-cnc-cad-and-cam-workflow/"
---

**Gist.** A parametric solid describes the finished part but says nothing about where the cutter travels, how fast it feeds, or how deep it plunges. The FreeCAD **CAM workbench** builds a second object graph — Job, Stock, Tool Controller, Operations — that references the modelled geometry and compiles to G-code through a post-processor for a specific controller dialect. The cost is a parallel model that must be kept consistent with the design model: geometry references are by name, so an edit that renames or removes a face invalidates the operation that selected it.

## Naming

Through the FreeCAD 0.x series this workbench was named **Path**. The term was overloaded within FreeCAD — sweep paths, file paths, toolpaths — and the 1.0 release renamed it to **CAM**. Documentation current as of **FreeCAD 1.1.3** (released July 2026) uses the new name. Older tutorials describing "the Path workbench" describe the same tool; **the Python module tree still uses `Path` internally**, so scripts import from `Path.Main`, not from a `CAM` package.

## The object tree

CAM is organised around one container, the **Job**, and the containment relation is what makes the tree recomputable.

A **Job** holds the machining program for one setup. Creation takes the **Model** (the solid to machine) and a **post-processor** (grbl, linuxcnc, and others), and auto-generates a **Stock** object and a **Setup Sheet**. Because the Job references the body rather than copying it, a recompute after a design change — a pocket deepened in Part Design — propagates into the toolpaths.

**Stock** represents the raw material bolted to the table. FreeCAD derives it from the model's bounding box plus margins, or accepts explicit dimensions. Stock is load-bearing for two reasons: **the emitted coordinates are relative to the Job's origin, so stock placement and that origin must agree with how the block is zeroed on the machine**, and the stock volume determines how much material the clearing operations must remove. Stock larger than the physical block produces air-cutting passes; stock smaller than the block leaves material the program never touches.

A **Tool Controller** binds a tool bit — a 6 mm flat endmill, for example — to its running parameters: spindle speed, horizontal feed, and vertical (plunge) feed. **Operations reference a Tool Controller rather than a bare tool**, which is the indirection that lets one Job drive several tools: the post-processor emits the corresponding tool change and the feed rates belonging to that controller.

**Operations** generate the toolpaths against selected model geometry:

- **Profile** (contour) — follows an edge or the outline of a face, cutting around it; this is the operation that separates the part from the stock.
- **Pocket Shape** — clears material inside a boundary in stepped-down passes.
- **Drilling** — canned drilling cycles on selected holes.
- **Adaptive** — clearing that holds a constant tool engagement angle rather than a constant stepover, which permits deeper axial cuts without the engagement spikes of a conventional zigzag entering a corner.
- **Face** — surfacing a top face flat.

## Two retract heights

Every operation exposes two Z values, and their relationship is the invariant that protects the cutter. **Safe Height** is the Z the tool retracts to for short rapid moves between cuts *within* an operation. **Clearance Height** is higher: the Z used for long rapids across the part, chosen to sit above clamps and fixtures.

The invariant the generated path maintains is that **lateral motion at rapid speed occurs only at or above these heights; below them motion is at a controlled feed rate**. The failure mode when the heights are set too low is direct: a rapid traverse at full machine speed passes through the workpiece or a clamp, with no feed limit to absorb the load.

## Dressups

A **dressup** wraps an existing operation and rewrites its path without regenerating the operation itself. Two are used routinely.

**Tags**, also called holding tabs, leave short uncut bridges in a Profile cut, so the part remains attached to the stock through the final pass instead of coming loose under the cutter. The bridges are broken by hand afterwards. **Ramp Entry** replaces a vertical plunge with an angled descent, so the cutter engages by shearing rather than by drilling on its centre, where a flat endmill has no effective cutting edge. Others exist — Dogbone for square-cornered slots, Lead In/Out — but tags and ramp entry cover the common cases.

## A pocket in a plate

Consider a parametric 80 x 60 x 10 mm aluminium plate carrying a 40 x 30 mm rectangular pocket 4 mm deep, already modelled in Part Design. The CAM half proceeds as follows.

1. Switch to **CAM**; with the body selected, **Job → Create Job**. Select the body as the model and the post-processor matching the controller: **grbl** for a hobby router, **linuxcnc** for a LinuxCNC machine.
2. Open the Job's **Stock** and confirm it matches the physical block (80 x 60 x 10, zero margins if the block was machined to size), and check where the Job's origin falls relative to it.
3. Add a **Tool Controller** for the 6 mm flat endmill with spindle speed, horizontal feed and plunge feed taken from a feeds-and-speeds reference for the material and machine.
4. Select the pocket floor face and add **Pocket Shape**. Set the final depth to −4 mm with a step-down per pass, a stepover fraction, and a zigzag pattern.
5. Add a **Ramp Entry** dressup to the operation so each level is entered at an angle.
6. Run **CAM → Simulator** to observe the removal and check for gouges.
7. **Post Process** the Job to an output file, for example `plate.nc`.

The grbl post-processor writes plain text. The clearing passes take this shape:

```gcode
(Begin operation: Pocket_Shape)
G90 G21              ; absolute, millimetres
M3 S10000            ; spindle on
G0 Z15.000           ; rapid to clearance height
G0 X20.000 Y15.000   ; rapid to pocket start
G1 Z-1.000 F250.000  ; ramp/plunge to first depth at plunge feed
G1 X60.000 F800.000  ; cutting move at horizontal feed
G1 Y45.000
G1 X20.000
G0 Z2.000            ; retract to safe height
...                  ; next stepdown, repeat to Z-4.000
G0 Z15.000           ; final retract to clearance
M5                   ; spindle off
M2                   ; program end
```

Two facts are visible in the output and worth checking before a cut. The two feed rates — `F250` on the Z plunge, `F800` on the XY cutting moves — originate in the Tool Controller, not in the operation. The two retract heights appear as distinct Z values: `Z2` between passes, `Z15` for the traverse at the end of the program.

The same graph can be built from the Python console rather than the GUI. `import Path.Main.Job as PathJob; job = PathJob.Create('Job', [body], None)` creates the Job, and operation modules under `Path.Main` add the Pocket. The emitted `.nc` file is plain text throughout, so inspecting it before the program reaches metal requires no additional tooling.

**Try next:** model a bolt-hole plate, add a Drilling operation together with a tag-dressed Profile to cut it free, post-process to the machine's dialect, and diff the resulting `.nc` against the pocket example above to see how each operation maps onto G-code.

## Pitfalls

- **Stock dimensions left at the bounding-box default while the physical block is oversized.** The program starts cutting below the real top surface, so the first pass takes a full-depth cut instead of the configured step-down.
- **Safe Height set below the top of a clamp.** A rapid traverse between cuts strikes the fixture at machine rapid speed; the feed rate limits in the operation apply only to `G1` moves, not to the `G0` that carries the collision.
- **Editing the design body so that a selected face is renumbered or removed.** The operation stores a geometry reference by name; after recompute it either points at a different face or fails, and the emitted path changes without an obvious warning.
- **Feeds and spindle speed edited on the operation instead of the Tool Controller.** The post-processor emits the Tool Controller's values, so the edited numbers do not appear in the `.nc` file.
- **A Profile operation without tags on a through-cut.** The part separates from the stock before the final pass completes and is free to move under the cutter.
- **Post-processing with the wrong dialect selected.** grbl and LinuxCNC accept overlapping but not identical command sets; a file built for one controller can be rejected or misinterpreted line by line by the other.
- **Trusting the simulator as a collision check.** It shows material removal against the model and stock; clamps, fixtures and the table are not part of that geometry.
