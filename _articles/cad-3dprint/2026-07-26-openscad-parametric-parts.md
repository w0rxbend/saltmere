---
title: "OpenSCAD: parametric enclosures as code for your ESP32 projects"
date: 2026-07-26
track: cad-3dprint
summary: "OpenSCAD models solids from primitives, CSG booleans, and transforms driven by named variables — so one diff-able script regenerates a whole family of sensor enclosures. A working parametric box module plus the CLI STL export with -D."
reading_time: 5
tags: [openscad, parametric, 3d-printing, enclosure, esp32, cad]
sources:
  - title: "OpenSCAD User Manual/Using OpenSCAD in a command line environment (WikiBooks)"
    url: "https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Using_OpenSCAD_in_a_command_line_environment"
  - title: "Using OpenSCAD in a command line environment (official docs mirror)"
    url: "https://files.openscad.org/documentation/manual/Using_OpenSCAD_in_a_command_line_environment.html"
  - title: "OpenSCAD Downloads (stable + nightly snapshots)"
    url: "https://openscad.org/downloads.html"
  - title: "OpenSCAD 2021.01 release (GitHub)"
    url: "https://github.com/openscad/openscad/releases/tag/openscad-2021.01"
  - title: "OpenSCAD User Manual/The OpenSCAD Language (WikiBooks)"
    url: "https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/The_OpenSCAD_Language"
---

If you build ESP32 and sensor projects, you eventually need a box. The board changes, the connector moves, you add a second sensor — and in a mouse-driven CAD tool you re-draw the whole thing. OpenSCAD takes the opposite stance: the model *is* a script. Change a variable, re-run, get a new part. This makes enclosures reproducible, version-controllable, and trivially retargetable to the next board.

## The mental model: solids from a functional script

OpenSCAD is a wrapper around a CSG (Constructive Solid Geometry) engine, written in C++, driven by its own declarative scripting language. You don't sketch and extrude interactively — you *describe* a solid as an expression tree and let the engine render it.

Three ingredients cover most of what you need:

| Ingredient | Keywords | What it does |
|---|---|---|
| Primitives | `cube`, `cylinder`, `sphere` | The raw solids |
| Boolean ops (CSG) | `union`, `difference`, `intersection` | Combine / subtract / overlap solids |
| Transforms | `translate`, `rotate`, `scale`, `mirror` | Position and orient |

The language is functional/declarative: variables are set once per scope (there is no imperative reassignment in the usual sense), and `module` definitions let you name a reusable piece of geometry with parameters. An enclosure becomes a `difference()` of an outer shell minus an inner cavity, plus a few mounting posts from a `module` — all driven by named variables at the top of the file.

## Why code-CAD instead of GUI parametric (FreeCAD)

GUI parametric tools like FreeCAD are powerful, but the parametric history is stored in an opaque document. The classic pain is **topological naming**: reference a face called `Face6`, then upstream geometry shifts and `Face6` now points somewhere else, silently breaking your model. OpenSCAD sidesteps this entirely — there are no persistent face IDs, only geometry recomputed from parameters every run.

| | OpenSCAD (code-CAD) | FreeCAD (GUI parametric) |
|---|---|---|
| Source of truth | Plain-text `.scad` | Binary document |
| Diff / code review | Yes, line-by-line | No |
| Version control | Native (git) | Blob only |
| Topological naming bugs | None | Possible |
| Batch a whole family | One CLI loop | Manual / macro |
| Freeform organic shapes | Awkward | Strong |

The trade-off is real: OpenSCAD is weaker at sculpted, spline-heavy shapes. For boxes, brackets, and standoffs — the bread and butter of sensor projects — the text-first approach wins.

## A parametric enclosure module

Here is a real, working script: an open-top enclosure sized from board dimensions, with wall thickness and four screw posts. Every dimension that might change is a named variable.

```scad
// ---- Parameters (edit these) ----
board_x     = 51;    // board length (mm)  e.g. ESP32 DevKit
board_y     = 28;    // board width  (mm)
board_h     = 1.6;   // PCB thickness
wall        = 2.0;   // wall + floor thickness
floor_gap   = 4;     // standoff height under the board
head_room   = 12;    // clearance above board for components
clearance   = 0.4;   // slack around the board (per side)
post_d      = 5;     // mounting-post outer diameter
post_hole_d = 2.6;   // pilot hole for M3 self-tapping screw
post_inset  = 3.5;   // board hole offset from its corner
$fn         = 48;    // curve smoothness

// ---- Derived ----
inner_x = board_x + 2 * clearance;
inner_y = board_y + 2 * clearance;
inner_z = floor_gap + board_h + head_room;
outer_x = inner_x + 2 * wall;
outer_y = inner_y + 2 * wall;
outer_z = inner_z + wall;          // floor only; top stays open

module mounting_post(h) {
    difference() {
        cylinder(d = post_d, h = h);
        translate([0, 0, wall])     // don't drill through the floor
            cylinder(d = post_hole_d, h = h);
    }
}

module enclosure() {
    difference() {
        cube([outer_x, outer_y, outer_z]);          // outer shell
        translate([wall, wall, wall])
            cube([inner_x, inner_y, inner_z + 1]);   // +1 opens the top
    }
    post_h = wall + floor_gap;                       // board sits on top
    for (x = [post_inset, board_x - post_inset])
        for (y = [post_inset, board_y - post_inset])
            translate([wall + clearance + x, wall + clearance + y, 0])
                mounting_post(post_h);
}

enclosure();
```

The whole part is one `difference()` (shell minus cavity) plus a `for`-nested set of posts. Want a taller box for a stacked shield? Bump `head_room`. New board? Change `board_x`/`board_y`. Nothing else moves by hand.

## Rendering and batch export from the CLI

The `openscad` binary runs headless. With `-o`, it skips the GUI and exports to a file whose **extension** selects the format — `stl`, `off`, `amf`, `3mf`, `csg`, `dxf`, `svg`, `png`, and more.

```bash
# Render the model as-is to STL
openscad -o enclosure.stl enclosure.scad
```

The `-D var=val` flag pre-defines (overrides) any top-level variable, and it can be repeated. This is what turns one script into a part *family* — no file editing, fully scriptable in CI:

```bash
# Retarget to a wider board with thicker walls, no source edits
openscad -o enclosure-wide.stl \
  -D 'board_x=70' -D 'board_y=50' -D 'wall=2.5' \
  enclosure.scad
```

```bash
# Generate a whole family from a table
for name in "esp32:51:28" "d1mini:34:26" "feather:51:23"; do
  IFS=: read tag bx by <<< "$name"
  openscad -o "case-$tag.stl" -D "board_x=$bx" -D "board_y=$by" enclosure.scad
done
```

One caveat from the manual: a variable overridden with `-D` must already exist in the script, and string values need their inner quotes escaped for the shell (e.g. `-D 'label="A1"'`).

## Version status

OpenSCAD's last tagged **stable** release is **2021.01** (January 2021) — unusually old for an active project. Development has continued steadily since; the project ships frequent **nightly/development snapshots** with newer features, and many makers run those in practice. For scripting fundamentals — primitives, CSG, `module`, and the `-o`/`-D` CLI — the stable release and the nightlies behave the same, so the script above is portable either way.

**Try next:** Add a `lid` module and a `part = "base"` variable, then render both halves from the same file with `openscad -o base.stl -D 'part="base"' case.scad` and `openscad -o lid.stl -D 'part="lid"' case.scad` — a two-piece snap case from one parametric source.
