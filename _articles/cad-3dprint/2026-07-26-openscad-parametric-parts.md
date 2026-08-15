---
title: "OpenSCAD: parametric enclosures as code for ESP32 projects"
date: 2026-07-26
track: cad-3dprint
summary: "OpenSCAD models solids from primitives, CSG booleans, and transforms driven by named variables, so one diff-able script regenerates a whole family of sensor enclosures. A working parametric box module plus the CLI STL export with -D."
reading_time: 6
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

**Gist.** An enclosure for a microcontroller board is a shape whose every dimension is a consequence of a handful of measurements, and those measurements change whenever the board, the connector position or the sensor count changes. OpenSCAD expresses the solid as a script over constructive solid geometry (CSG) — primitives combined by boolean operations and positioned by transforms, all driven by named variables — so a dimension change is a one-line edit and the geometry is recomputed from scratch. The cost is that nothing is recomputed *incrementally* and nothing is drawn interactively: every render re-evaluates the whole expression tree, and shapes that are naturally described by sculpted surfaces rather than by boolean algebra are awkward to express at all.

## The evaluation model: solids as an expression tree

OpenSCAD is a front end to a CSG engine, written in C++ and driven by its own declarative scripting language. There is no sketch-and-extrude interaction; a solid is *described* as an expression tree and the engine renders it.

Three ingredients cover most enclosure work:

| Ingredient | Keywords | Effect |
|---|---|---|
| Primitives | `cube`, `cylinder`, `sphere` | The raw solids |
| Boolean operations (CSG) | `union`, `difference`, `intersection` | Combine, subtract, overlap solids |
| Transforms | `translate`, `rotate`, `scale`, `mirror` | Position and orient |

The language is declarative rather than imperative. **A variable is bound once per scope**, so a name has a single value throughout the scope in which it appears regardless of textual order — a script cannot accumulate state by reassigning a variable inside a loop. `module` definitions name a parametrised piece of geometry that can be instantiated repeatedly. Under this model an enclosure is a `difference()` of an outer shell minus an inner cavity, unioned with mounting posts produced by a `module`, with every magnitude traced back to a named variable at the top of the file.

The invariant that makes the approach robust: **the output is a pure function of the top-level variable bindings**. Two runs with the same bindings yield the same geometry; no state persists between runs.

## Code-CAD compared with graphical parametric CAD

A graphical parametric tool such as FreeCAD stores its parametric history inside a document format that is not plain text. The failure mode with a name is **topological naming**: a feature references a face by an identifier such as `Face6`, upstream geometry then shifts, the identifier is reassigned to a different face, and the downstream feature silently attaches to the wrong place. **OpenSCAD has no persistent face identifiers at all** — geometry is recomputed from parameters on every run, so there is no identifier that can be reassigned. The class of failure does not exist in this model.

| | OpenSCAD (code-CAD) | FreeCAD (graphical parametric) |
|---|---|---|
| Source of truth | Plain-text `.scad` | Binary document |
| Diff / code review | Line-by-line | Not textual |
| Version control | Native (git) | Blob only |
| Topological naming bugs | None | Possible |
| Batch a whole family | One command-line loop | Manual or macro |
| Freeform organic shapes | Awkward | Strong |

The trade-off is genuine: shapes dominated by splines and sculpted surfaces are hard to state as boolean combinations of primitives. Boxes, brackets and standoffs — the recurring parts of sensor projects — decompose into CSG cleanly.

## A parametric enclosure module

An open-top enclosure sized from board dimensions, with wall thickness and four screw posts. Every dimension liable to change is a named variable; every other magnitude is derived from those.

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
        translate([0, 0, wall])     // start the pilot hole above the floor
            cylinder(d = post_hole_d, h = h);
    }
}

module enclosure() {
    difference() {
        cube([outer_x, outer_y, outer_z]);           // outer shell
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

Two details carry the model. The cavity cube is **one millimetre taller than `inner_z`**, so the subtracted volume protrudes past the top face and leaves an open top rather than a zero-thickness lid; a subtraction that ends exactly at the boundary leaves two coincident faces, and the user manual calls an overlap on the removed solid mandatory for that reason. The pilot hole in `mounting_post` is **translated up by `wall`**, so the drilled cylinder starts at the top of the floor and the floor stays continuous underneath.

Increasing `head_room` raises the box, since `inner_z` and then `outer_z` are derived from it. Changing `board_x` and `board_y` moves the walls and, through the `for` ranges, the posts together.

## Rendering and batch export from the command line

The `openscad` binary runs headless. With `-o` it skips the graphical interface and exports to a file whose **extension selects the format** — `stl`, `off`, `amf`, `3mf`, `csg`, `dxf`, `svg`, `png` among others.

```bash
# Render the model as-is to STL
openscad -o enclosure.stl enclosure.scad
```

The `-D var=val` flag pre-defines, and thereby overrides, a top-level variable, and may be repeated. This is the mechanism that turns one script into a part *family* without editing the source, which in turn makes the render step reproducible in continuous integration:

```bash
# Retarget to a wider board with thicker walls, no source edits
openscad -o enclosure-wide.stl \
  -D 'board_x=70' -D 'board_y=50' -D 'wall=2.5' \
  enclosure.scad
```

```bash
# Generate a whole family from a table
for name in "esp32:51:28" "d1mini:34:26" "feather:51:23"; do
  IFS=: read -r tag bx by <<< "$name"
  openscad -o "case-$tag.stl" -D "board_x=$bx" -D "board_y=$by" enclosure.scad
done
```

Two constraints govern `-D`. **The assignment only has an effect where the script reads a top-level variable of that name**: a name the script never mentions is defined and then ignored. And a string value needs its inner quotes protected from the shell (`-D 'label="A1"'`), since the shell strips unquoted quotes before OpenSCAD parses the expression.

## Version status

The last tagged **stable** release is **2021.01** (January 2021). Development has continued since, and the project ships **nightly development snapshots** with newer features. For the scripting fundamentals used above — primitives, CSG operators, `module`, and the `-o` and `-D` command-line flags — the stable release and the snapshots behave the same, so the script is portable across both.

A natural extension: add a `lid` module and a `part = "base"` variable, then render both halves from one file with `openscad -o base.stl -D 'part="base"' case.scad` and `openscad -o lid.stl -D 'part="lid"' case.scad`.

## Pitfalls

- **A subtracted cube whose face lands exactly on the shell face produces coincident, zero-thickness geometry.** The overlap term (`inner_z + 1` above) exists to push the cut clear of the boundary; without it the two surfaces occupy the same plane, which the preview renders as flickering artifacts and which leaves the result at that face ill-defined.
- **An assignment inside a `for` body does not carry to the next iteration.** A variable is bound once per scope, so a script written to accumulate a running total across iterations produces geometry from the original binding instead, and reports no error.
- **`-D` on a name not present at top level has no effect and reports no error.** A typo in a batch script yields a full family of parts rendered with default dimensions.
- **A string passed with `-D` loses its quotes to the shell unless they are protected.** `-D part=base` passes a bare identifier rather than the string `"base"`, so a comparison against `"base"` in the script fails.
- **`$fn` controls facet count on every curved primitive in scope.** A high value chosen for a visible fillet also applies to every screw hole, and the facet count multiplies through the mesh, so render time grows with a setting that was intended for one feature.
- **OpenSCAD is weak on spline-driven surfaces.** A shape that must be described by sculpted freeform surfaces rather than by boolean combinations of primitives will be laborious to express here regardless of parametrisation.
