---
title: "From parametric body to G-code: the FreeCAD CAM workbench"
date: 2026-08-04
track: cad-3dprint
summary: "You already have the parametric part. The CAM workbench (renamed from Path in FreeCAD 1.0) turns that solid into toolpaths and a grbl or LinuxCNC .nc file your mill can actually run."
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

Everywhere else on this track we treat FreeCAD as a modeller: sketch, pad, parametrise, regenerate. But if you own a CNC mill or router, the model is only half the job. The other half is telling the machine where to put the cutter, how fast to feed it, and how deep to plunge — and emitting that as G-code the controller understands. That is the **CAM workbench**, and it reads the parametric body you already built. Change a pocket depth in Part Design, recompute, and the toolpath follows.

One naming note first, because it trips people up. Through FreeCAD 0.x this was the **Path** workbench. The word "path" was badly overloaded in FreeCAD — sweep paths, file paths, toolpaths — so for the 1.0 release it was renamed to **CAM**, the term anyone doing machining already expects. This is current as of **FreeCAD 1.1.3** (July 2026). If you follow an older tutorial that says "Path workbench," it is the same tool; menus and the Python module tree still carry `Path` internally.

## The object tree: Job, Stock, Tool Controller, Operations

CAM is organised around one container object, the **Job**. Everything else lives inside it, so the mental model is worth getting straight before you click anything.

A **Job** wraps the whole machining program for one setup. When you create it you pick the **Model** (your solid) and the **post-processor** (grbl, LinuxCNC, and others), and it auto-generates a **Stock** and a **Setup Sheet**.

**Stock** is the raw material — the block you actually bolt to the table. FreeCAD derives it from the model's bounding box plus margins, or you give it explicit dimensions. It matters because it defines the top surface (your Z origin) and how much material the toolpaths have to clear.

A **Tool Controller** binds a physical **tool bit** (a 6 mm flat endmill, say) to its running parameters: spindle RPM, horizontal feed, vertical (plunge) feed. Operations reference a Tool Controller rather than a bare tool, so one job can drive several tools and the post-processor emits the right tool change and feeds for each.

**Operations** are the verbs. Each generates toolpaths against the model geometry:

- **Profile** (contour) — follows an edge or the outline of a face, cutting *around* it. This is how you cut a part free from the stock.
- **Pocket Shape** — clears the material *inside* a boundary, in stepped-down passes.
- **Drilling** — canned drilling cycles on selected holes.
- **Adaptive** — high-efficiency clearing that keeps a constant tool engagement angle, letting you take deep, fast cuts without shock-loading the cutter.
- **Face** — surfacing a top face flat.

## Heights, and why they keep you from breaking cutters

Two Z values appear on every operation and get ignored by beginners at the cost of snapped endmills. **Safe Height** (also called clearance) is the Z the tool retracts to for short rapid moves between cuts within the operation. **Clearance Height** is higher still — the Z for long rapids across the whole part, guaranteed above clamps and fixtures. The rule: the tool only ever travels sideways at rapid speed at or above these heights; below them it moves at controlled feed. Set them too low and a rapid drags the cutter through your workpiece or a clamp.

## Dressups: modifying a path without redrawing it

A **dressup** wraps an existing operation and alters its path. Two you will use constantly:

**Tags** (holding tabs) leave small uncut bridges on a Profile cut so the part does not break loose and get flung across the shop on the final pass. You snap the part off by hand afterward. **Ramp Entry** replaces a straight vertical plunge with a shallow angled descent, so the cutter shears into material instead of drilling straight down — kinder to endmills that cut poorly on their centre. Others exist (Dogbone for slotting square corners, Lead In/Out) but tags and ramp are the everyday two.

## A concrete pocket in a plate

Say you have a parametric 80 x 60 x 10 mm aluminium plate with a 40 x 30 mm, 4 mm-deep rectangular pocket already modelled in Part Design. Here is the CAM half.

1. Switch to **CAM**. With the body selected, **Job → Create Job**. Pick the body as the model; choose **grbl** as the post-processor if you run a hobby router, or **linuxcnc** for a LinuxCNC box.
2. Open the Job's **Stock** and confirm it matches your real block (80 x 60 x 10, zero margins if you machined it to size). The Z origin sits on the stock top.
3. **Add a Tool Controller** with a 6 mm flat endmill. Set spindle to ~10000 RPM, horizontal feed ~800 mm/min, plunge ~250 mm/min for aluminium — tune to your machine and read a real feeds-and-speeds chart before cutting.
4. Select the pocket floor face, then **Pocket Shape**. Set Final Depth to −4 mm, a step-down of ~1 mm, and a 50% stepover with a zigzag pattern.
5. Right-click the operation, **Add Ramp Entry Dressup**, so it eases into each level.
6. **CAM → Simulator** to watch the cut and confirm nothing gouges.
7. **Post Process** the Job. Choose the output file, e.g. `plate.nc`.

The grbl post-processor writes a plain text file. The clearing passes for that pocket look like this:

```gcode
(Begin operation: Pocket_Shape)
G90 G21              ; absolute, millimetres
M3 S10000            ; spindle on, 10000 rpm
G0 Z15.000           ; rapid to clearance height
G0 X20.000 Y15.000   ; rapid to pocket start
G1 Z-1.000 F250.000  ; ramp/plunge to first depth at plunge feed
G1 X60.000 F800.000  ; cutting move at horizontal feed
G1 Y20.000
G1 X20.000
G0 Z2.000            ; retract to safe height
...                  ; next stepdown, repeat to Z-4.000
G0 Z15.000           ; final retract to clearance
M5                   ; spindle off
M2                   ; program end
```

Note the two speeds — `F250` on the Z plunge, `F800` on the XY cutting — coming straight from the Tool Controller, and the two retract heights (`Z2` safe, `Z15` clearance).

If you would rather script the job than click it, the whole thing is Python. From the console, `import Path.Main.Job as PathJob; job = PathJob.Create('Job', [body], None)` builds the Job; operation modules under `Path.Main` add the Pocket. But for a first cut, drive the GUI, watch the simulator, and read the emitted `.nc` before it ever touches metal — the G-code is human-readable for exactly this reason.

**Try next:** model a bolt-hole plate, add a Drilling operation plus a tag-dressed Profile to cut it free, post-process to your machine's dialect, and diff the `.nc` against this pocket example to see how each operation maps to G-code.
