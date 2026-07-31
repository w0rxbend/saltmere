---
title: "BOSL2: the OpenSCAD library that makes enclosures, threads, and rounding trivial"
date: 2026-07-31
track: cad-3dprint
summary: "Vanilla OpenSCAD makes you track every coordinate by hand and reinvent fillets and threads each project. BOSL2 replaces all of that with attachments, rounded primitives, and a real threading library — so a printed ESP32 enclosure with a screw-on lid becomes a dozen readable lines."
reading_time: 5
tags: [openscad, bosl2, code-cad, enclosures, 3d-printing]
sources:
  - title: "BOSL2 — The Belfry OpenSCAD Library v2.0 (GitHub)"
    url: "https://github.com/BelfrySCAD/BOSL2"
  - title: "BOSL2 threading.scad — threaded_rod, screws, bottle/pipe threads (wiki)"
    url: "https://github.com/BelfrySCAD/BOSL2/wiki/threading.scad"
  - title: "Belfry OpenSCAD Library (BOSL2) Brings Useful Parts and Tools Aplenty — Hackaday"
    url: "https://hackaday.com/2025/02/18/belfry-openscad-library-bosl2-brings-useful-parts-and-tools-aplenty/"
---

Plain OpenSCAD is wonderfully honest and quietly exhausting. Want a box with rounded edges, a lid that positions itself on top, and a threaded hole for a brass insert? You'll be doing `translate([0,0,wall+h/2])` arithmetic in your head, faking fillets with `minkowski()`, and copy-pasting somebody's thread module. **BOSL2** — the Belfry OpenSCAD Library, v2.0 (still officially beta, and requiring OpenSCAD **2021.01 or newer**) — replaces all of that with three things worth learning: attachments, rounded primitives, and real threads.

## Attachments: stop tracking coordinates

The headline feature. Instead of translating a child into place, you *attach* it to an anchor point on its parent. Every BOSL2 shape exposes named anchors (`TOP`, `BOTTOM`, `LEFT`, `RIGHT`, `FWD`, `BACK`) plus `spin` and `orient`. You say "put this on top of that," and BOSL2 does the geometry:

```scad
include <BOSL2/std.scad>

cuboid([60, 40, 25], rounding = 3, edges = "Z") {   // rounded vertical edges
    attach(TOP) cuboid([60, 40, 3]);                 // lid sits on top, centered
    attach(FRONT) cyl(h = 4, d = 12);                // a boss on the front face
}
```

No `translate`, no adding half-heights. Move the box and the lid and boss follow, because they're defined *relative* to it. On any model with more than one part, this alone changes how it feels to work.

## Rounded primitives and masks

`cuboid()` takes a `rounding` argument directly — no Minkowski tax, and you can restrict it to specific edges (`edges = "Z"` for just the vertical ones, so the box has a flat top and bottom for printing). For more control there's `offset_sweep()` to round the ends of an extrusion and `edge_profile()` to run a custom fillet or chamfer profile along chosen edges. Add the shorthands — `up(z)`, `left(x)`, `fwd(y)` in place of verbose `translate([...])` — and the code reads like the shape you're describing.

## Threads that actually work

BOSL2's `threading.scad` gives you `threaded_rod()`, `threaded_nut()`, and screw utilities, plus dedicated bottle- and pipe-thread modules. So a screw-together enclosure isn't a research project:

```scad
include <BOSL2/std.scad>
include <BOSL2/threading.scad>

difference() {
    cuboid([50, 50, 20], rounding = 2, edges = "Z");
    // subtract an M8 threaded hole through the middle for a bolt/insert
    threaded_rod(d = 8, l = 22, pitch = 1.25, internal = true, $fn = 48);
}
```

`internal = true` cuts a matching internal thread, so the printed part accepts a real M8 bolt straight off the bed (tune `$fn` and your slicer's horizontal expansion for fit). The parts library goes well beyond screws — gears, hinges, clips, dovetails — but threads are the thing you'll reach for constantly on sensor enclosures.

## Getting it

Download the repo and unpack it into your OpenSCAD libraries folder — on Linux that's `$HOME/.local/share/OpenSCAD/libraries/BOSL2/`; on Fedora you can just `sudo dnf install openscad-bosl2`. Then `include <BOSL2/std.scad>` at the top of your file. One caveat to save you confusion: **BOSL v1 code does not run on BOSL2** — the APIs differ, so ignore old v1 snippets you find online and stick to the v2 wiki.

**Try next:** model a two-part enclosure for one of your ESP32 boards — a rounded `cuboid()` base with `attach()`ed standoffs for the mounting holes, and a lid that attaches to `TOP` with four `threaded_rod(internal=true)` holes for M3 screws. Print the base and drop your board in; getting the standoff spacing right in code the first time is the moment attachments sell themselves.
