---
title: "Gridfinity: Parametric Storage as an Engineering Exercise"
date: 2026-08-15
track: cad-3dprint
summary: "Zack Freedman's Gridfinity is a deceptively simple spec — 42 mm grid, 7 mm height units, a stepped base profile that nests into baseplate sockets — that has grown a genuine parametric ecosystem. Here's the geometry that makes bins stack and locate, which generators are actually maintained in 2026 (gridfinity-rebuilt-openscad v2 leads), a working OpenSCAD snippet for a custom bin, and how to design tool and PCB inserts instead of downloading someone else's."
reading_time: 5
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

Zack Freedman released Gridfinity in 2022 as an open standard for workshop storage, and it has outlived the YouTube hype cycle for a simple reason: it's a *spec*, not a product. Baseplates tile your drawer; bins click into the baseplate on a grid; everything from anyone's generator interoperates. That makes it an unusually good exercise in parametric CAD — the constraints are published numbers, and your job is to generate geometry that honors them.

## The geometry that makes it work

The core numbers: a **42 × 42 mm grid pitch**, bins with a **41.5 mm footprint** (0.5 mm total clearance so a 5-wide bin doesn't bind), and heights in **7 mm units**. A "3U" bin is 21 mm tall, plus a ~**4.4 mm stacking lip** on top that isn't counted in the height — the lip's inner profile matches the base profile of the bin stacked above it, which is why any bin stacks on any other.

The clever part is the **base profile**: each 42 mm cell of a bin's underside is a stepped, chamfered pyramid that drops into a matching socket in the baseplate. The taper self-centers as it seats, so bins locate positively but lift out without a fight — the same trick as a machinist's pallet system, executed in PLA. Retention is optional and layered: each base cell has four corner positions accepting **6 × 2 mm disc magnets**, and the same positions can instead take **M3 screws** for bolting bins (or baseplates) down hard. Magnets in the baseplate plus magnets in the bin gives click-in retention that survives a drawer slam; screws are for the bins you never want walking, like a vise-mounted parts tray.

| Spec item | Value |
|---|---|
| Grid pitch | 42 × 42 mm |
| Bin footprint | 41.5 × 41.5 mm per cell |
| Height unit | 7 mm (bin = U × 7 + lip) |
| Stacking lip | ~4.4 mm nominal |
| Magnet pocket | 6 mm dia × 2 mm |
| Screw option | M3 |

## The generator ecosystem in 2026

Because the spec is open, generators multiplied. What's actually alive today: **gridfinity-rebuilt-openscad** (kennetek, MIT) is the reference implementation — a ground-up mathematical rebuild that hit **v2.0.0 in September 2025** and remains actively developed, generating bins, solid bins, dividers, holes, vase-mode variants, and baseplates. For click-not-code workflows there are web generators (the Perplexing Labs generator being the best known) and native options: a FreeCAD Gridfinity workbench in the Addon Manager and Fusion 360 generator add-ins. The **gridfinity.tools** directory and Stu142's Gridfinity-Documentation repo are the fastest way to find a current generator for your CAD of choice, and gridfinity.xyz keeps an unofficial but thorough write-up of the spec itself.

## A custom bin in OpenSCAD

Clone gridfinity-rebuilt and either drive `gridfinity-rebuilt-bins.scad` from the OpenSCAD customizer, or script it. The v2 API builds a bin object, then renders it with compartment cutters:

```openscad
include <src/core/standard.scad>
use <src/core/gridfinity-rebuilt-utility.scad>
use <src/core/gridfinity-rebuilt-holes.scad>
use <src/core/bin.scad>
use <src/core/cutouts.scad>

// (refined, magnet, screw, crush_ribs, chamfer, printable_top)
hole_options = bundle_hole_options(false, true, false, true, true, true);

bin = new_bin(
    grid_size = [3, 2],               // 3 x 2 cells = 126 x 84 mm
    height_mm = height(6, 0, false),  // 6U: 6 x 7 mm, lip excluded
    include_lip = true,
    hole_options = hole_options
);

bin_render(bin)
    bin_subdivide(bin, [3, 1])        // three compartments along X
        cut_compartment_auto(cgs(), 1, false, 1);  // auto tab, full scoop
```

`height(gridz, gridz_define, zsnap)` is worth understanding: `gridz_define` selects whether your number means 7 mm units, internal millimeters, or overall millimeters — the difference between "a 6U bin" and "a bin whose cavity fits a 38 mm tall part." The **scoop** (a radiused floor at the front wall) and **label tab** are per-compartment; magnet pockets get **crush ribs** so magnets press-fit without glue, and `printable_top` bridges the pocket ceiling so no supports are needed.

Print settings are undemanding: 0.2 mm layers, two perimeters, 10–15 % infill (bins are nearly all wall anyway), no supports if you keep the printable hole options on. PLA or PETG both work; what matters is first-layer accuracy, since an over-squished first layer fattens the base profile and makes bins tight in the sockets. Pause-at-height for magnets is obsolete — press them into the ribbed pockets after printing.

## The actual exercise: custom inserts

Downloading bins is shopping; designing inserts is engineering. The pattern: start from a solid bin (`divx = 0` in the customizer, or skip subdivision in code) and subtract your own cavities — `cut_chamfered_cylinder()` for driver shafts and collets, rectangular pockets with a finger relief for calipers, and for PCBs a pocket at board outline +0.3 mm with corner standoffs so components hang free and a thumb notch on one edge. Measure the tool, add 0.2–0.4 mm clearance depending on how snug you want it, and let the grid handle everything else — footprint, stacking, and retention are already solved by the spec. That division of labor is the whole appeal: the standard does the system engineering, you do the last 5 % that fits *your* tools.

**Try next:** pick the worst drawer in your shop, print one 4×4 baseplate and a scripted 3×2×6U bin with magnet holes, and design one custom insert for the tool you reach for most — one evening, and you'll understand why the spec won.
