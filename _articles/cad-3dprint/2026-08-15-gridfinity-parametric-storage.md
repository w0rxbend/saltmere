---
title: "Gridfinity: Parametric Storage as an Engineering Exercise"
date: 2026-08-15
track: cad-3dprint
summary: "Gridfinity is a published storage specification — 42 mm grid pitch, 7 mm height units, a stepped base profile that nests into baseplate sockets — with a parametric generator ecosystem around it. This article covers the geometry that makes bins stack and locate, the generators maintained as of 2026 (gridfinity-rebuilt-openscad v2 as reference implementation), a working OpenSCAD script for a custom bin, and the design of tool and PCB inserts."
reading_time: 6
tags: [gridfinity, openscad, parametric, 3d-printing, storage, workshop]
sources:
  - title: "gridfinity-rebuilt-openscad — kennetek (GitHub)"
    url: "https://github.com/kennetek/gridfinity-rebuilt-openscad"
  - title: "Gridfinity Rebuilt documentation"
    url: "https://kennetek.github.io/gridfinity-rebuilt-openscad/"
  - title: "Gridfinity unofficial specification wiki"
    url: "https://gridfinity.xyz/"
  - title: "Gridfinity-Documentation — Stu142 (GitHub)"
    url: "https://github.com/Stu142/Gridfinity-Documentation"
  - title: "gridfinity.tools — generator and resource directory"
    url: "https://gridfinity.tools/"
---

**Gist.** Workshop storage printed ad hoc produces containers that do not tile, do not stack, and do not interoperate between designs. Gridfinity, released by Zack Freedman in 2022 as an open standard, fixes the problem by publishing a dimensional contract — grid pitch, height quantum, base profile, retention feature positions — so that any generator's output nests with any other's. The cost is that every dimension a designer would otherwise choose freely is now fixed, and parts print correctly only when the printer holds the dimensional tolerance the contract assumes.

## The geometry that enforces interoperability

The specification is a small set of published numbers. The grid pitch is **42 × 42 mm**. A bin occupies an integer number of cells, and each cell's footprint is **41.5 × 41.5 mm**, leaving **0.5 mm of total clearance per cell pitch** so that a multi-cell bin does not bind against neighbouring sockets as cell count grows. Heights are quantised in **7 mm units**: a 3U bin is 21 mm of body. On top of the body sits a **stacking lip of approximately 4.4 mm nominal**, which is excluded from the stated height.

The lip is the load-bearing invariant. Its inner profile is the same profile as a bin's base. Because the two profiles match, **the mating surface between any two bins is identical regardless of which bins they are**, so stacking is a property of the specification rather than of a particular model. A generator that reproduces the lip profile correctly produces bins that stack with a bin printed by a different generator on a different printer.

The **base profile** is the second mechanism. Each 42 mm cell of a bin's underside is a stepped, chamfered pyramid that descends into a matching socket in the baseplate. Because the walls taper, contact during insertion begins at the widest part and progressively centres the bin as it descends — positive location on seating, and release without interference on lift-out. The taper does the alignment; no fastener is required for the bin to sit in a defined position.

Retention beyond gravity is optional and layered on top of the base profile. Each base cell carries **four corner positions**, each of which accepts either a **6 mm diameter × 2 mm disc magnet** or an **M3 screw**. Magnets in the baseplate paired with magnets in the bin give a retention force that resists a drawer being slammed shut. Screws instead fix the bin or baseplate rigidly, which is the option for a tray that must not move at all, such as one mounted at a vise.

| Spec item | Value |
|---|---|
| Grid pitch | 42 × 42 mm |
| Bin footprint | 41.5 × 41.5 mm per cell |
| Height unit | 7 mm (bin = U × 7 + lip) |
| Stacking lip | ~4.4 mm nominal |
| Magnet pocket | 6 mm dia × 2 mm |
| Screw option | M3 |

## The generator ecosystem as of 2026

Because the specification is open, multiple independent generators exist. **gridfinity-rebuilt-openscad** (kennetek, MIT licence) serves as the reference implementation: a ground-up mathematical rebuild, now on its second major version, that remains under active development. It generates bins, solid bins, dividers, hole variants, vase-mode variants, and baseplates.

For workflows without scripting, web generators exist — the Perplexing Labs generator among them — alongside native CAD options: a Gridfinity workbench available through the FreeCAD Addon Manager, and generator add-ins for Fusion 360. The **gridfinity.tools** directory and Stu142's Gridfinity-Documentation repository index current generators by CAD package. The gridfinity.xyz wiki maintains an unofficial but detailed write-up of the specification itself; it is not published by the standard's author, which is the reason to treat its numbers as a secondary rather than a primary source.

## A custom bin in OpenSCAD

The application programming interface (API) separates the bin body from the cavities cut into it: one module emits the body, and the compartment cutters are passed to it as children. The script below drives the library programmatically rather than through the OpenSCAD customizer.

```openscad
include <src/core/standard.scad>
use <src/core/gridfinity-rebuilt-utility.scad>
use <src/core/gridfinity-rebuilt-holes.scad>

// (refined, magnet, screw, crush_ribs, chamfer, supportless)
hole_options = bundle_hole_options(false, true, false, true, true, true);

// 3 x 2 cells = 126 x 84 mm; 6U body, stacking lip excluded from the count
gridfinityInit(3, 2, height(6, 0, 1, true)) {
    // three compartments along X, automatic label tab, full scoop
    cutEqual(n_divx = 3, n_divy = 1, style_tab = 1, scoop_weight = 1);
}

gridfinityBase([3, 2], hole_options = hole_options);
```

The `height()` call is the one that most often produces an unexpected result. Its second argument, `gridz_define`, **selects the interpretation of the first**: whether the supplied number denotes 7 mm height units, internal cavity millimetres, or overall millimetres. The distinction separates the request "a 6U bin" from the request "a bin whose cavity accommodates a 38 mm tall part"; the two produce different geometry from the same numeral.

The **scoop** — a radiused floor blending into the front wall — and the **label tab** are per-compartment features, set by the cutter rather than by the bin. Two hole options change what the printer must do: **crush ribs** narrow the magnet pocket with sacrificial features so the magnet press-fits without adhesive, and **supportless** bridges the pocket ceiling so the pocket prints without support material.

Print settings are undemanding: 0.2 mm layers, sparse infill, and no support material provided the supportless hole option remains enabled. Polylactic acid (PLA) and polyethylene terephthalate glycol (PETG) both work. The parameter that determines whether bins fit is **first-layer accuracy**: an over-squished first layer widens the base profile outward, and the widened profile binds in the baseplate socket. Pausing the print at magnet height is unnecessary when crush ribs are enabled, since magnets are pressed in after printing.

## Designing inserts

Downloading published bins exercises none of the specification; designing inserts does. The pattern is to begin from a solid bin — `divx = 0` in the customizer, or omitting the subdivision step in a script — and subtract cavities shaped to the tool. A chamfered cylinder produces the pockets for driver shafts and collets. Calipers take a rectangular pocket with a finger relief so the tool can be lifted out. A printed circuit board (PCB) takes a pocket at the board outline plus a clearance, with corner standoffs so that components on the underside hang clear of the floor, and a thumb notch on one edge for extraction.

Clearance is the only dimension the designer chooses: **0.2–0.4 mm** over the measured tool, the lower end for a retaining fit and the upper end for easy removal. Footprint, stacking, and retention are fixed by the specification and require no decision. That division is the appeal of the standard: the interoperability problem is solved once, in public, and the remaining work is the geometry specific to one set of tools.

## Pitfalls

- **A bin printed with an over-squished first layer will not seat in the baseplate.** Elephant's foot on the first layer widens the base profile beyond the 41.5 mm cell footprint, consuming the 0.5 mm clearance and causing interference in the socket.
- **A height number passed with the wrong `gridz_define` yields a bin of the wrong size that still prints cleanly.** The argument reinterprets the same numeral as height units, internal millimetres, or overall millimetres; the error is silent because every interpretation is valid geometry.
- **Stating a bin's height as U × 7 mm and then allowing that value for drawer clearance underestimates the part.** The stacking lip, approximately 4.4 mm nominal, sits above the counted height and is excluded from it.
- **Magnet pockets generated without the supportless option require support material.** The pocket ceiling is otherwise an unsupported horizontal span, and printing it without support produces a drooped ceiling that changes pocket depth.
- **Magnet pockets generated without crush ribs give a loose fit.** The nominal 6 mm pocket plus printer tolerance exceeds the magnet diameter, so the magnet does not retain without adhesive.
- **A multi-cell bin binds if per-cell clearance is treated as per-bin clearance.** The 0.5 mm is allocated per 42 mm pitch; a design that subtracts it once from the overall outline leaves the interior cells oversized.
- **The gridfinity.xyz specification wiki is unofficial.** It is a community reconstruction rather than a publication of the standard's author, and a dimension taken from it has not been checked against a primary source.
