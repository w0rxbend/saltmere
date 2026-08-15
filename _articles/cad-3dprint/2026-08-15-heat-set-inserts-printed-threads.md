---
title: "Heat-Set Inserts vs Printed Threads: Fastening Parts That Come Apart"
date: 2026-08-15
track: cad-3dprint
summary: "A printed M3 thread strips after a handful of cycles; a brass heat-set insert in the same boss survives hundreds and pulls out at well over 1000 N in PLA. When printed threads genuinely win (coarse, large, BOSL2-generated), how to size the boss — ~4.0 mm hole for a Ruthex RX-M3x5.7, wall over 2 mm, depth one insert-length plus 1 mm — and what CNC Kitchen's pull-out testing actually measured."
reading_time: 5
tags: [heat-set-inserts, threads, 3d-printing, fasteners, openscad, bosl2, design]
sources:
  - title: "Threaded Inserts for 3D Prints — Cheap vs Expensive — CNC Kitchen"
    url: "https://www.cnckitchen.com/blog/threaded-inserts-for-3d-prints-cheap-vs-expensive"
  - title: "Are Our Heat-Set Insert Datasheets Wrong? — CNC Kitchen"
    url: "https://www.cnckitchen.com/blog/are-our-heat-set-insert-datasheets-wrong"
  - title: "ruthex M3 thread insert RX-M3x5.7 — product page with CAD data"
    url: "https://www.ruthex.de/en/products/ruthex-gewindeeinsatz-m3-100-stuck-rx-m3x5-7-messing-gewindebuchsen"
  - title: "BOSL2 threading.scad — threaded rods and screw holes (wiki)"
    url: "https://github.com/BelfrySCAD/BOSL2/wiki/threading.scad"
  - title: "3D Printing Threads and Adding Threaded Inserts — Formlabs"
    url: "https://formlabs.com/blog/adding-screw-threads-3d-printed-parts/"
---

The lid of an enclosure you open once can be held by anything. The lid you open every week — to swap a sensor, reflash over pogo pins, replace a battery — needs a fastener that survives repeated cycles, and this is where 3D-printed projects quietly fail. A screw driven into bare plastic, or into a printed M3 thread, works beautifully for the first few insertions and then strips, because each cycle shaves the same soft flanks. The two durable answers are **printed threads at coarse pitch and large diameter**, and **brass heat-set inserts** for everything small. Knowing which one a joint wants is a design decision, not a preference.

## When printed threads win

FDM resolves a thread form well once the pitch is large relative to the nozzle: from about **M8 upward** (or any custom coarse/trapezoidal profile) printed threads are strong, free, and need no hardware. Jar-style lids, sensor-pod collars, knurled thumbwheels — all better printed. In OpenSCAD, [BOSL2's](/articles/cad-3dprint/2026-07-31-bosl2-openscad-library/) `threading.scad` generates matching internal/external pairs with a working `$slop` clearance in two lines, and its bottle- and pipe-thread modules exist precisely because coarse profiles print reliably. Print vertical-axis threads, add a small lead-in chamfer, and expect a break-in turn or two. What printed threads are *bad* at is small metric sizes under repeated use: an M3 printed thread has flanks a fraction of a layer tall and strips at roughly 1 Nm.

## The heat-set boss, sized correctly

A **heat-set insert** is a knurled brass bushing melted into an undersized hole; the plastic flows into the knurl and, once solid, mechanically locks it — high-speed footage from CNC Kitchen shows the plastic never adheres, it *interlocks*. That makes the boss geometry the whole game:

- **Hole diameter:** per the insert spec. For the ubiquitous **Ruthex RX-M3x5.7** (M3 × 5.7 mm long), the manufacturer's figure is a **4.0 mm** hole. CNC Kitchen's diameter sweep found peak pull-out around 4.0–4.1 mm *actual*, and recommends modeling **4.2 mm in CAD** to compensate for the ~0.25 mm a printed hole typically shrinks — the criterion is that the insert pre-seats with slight resistance. At 4.2 mm actual you still keep ~90% of maximum strength; oversize further and strength falls off a cliff.
- **Hole depth:** insert length **plus at least 1 mm** of clear bore below, so displaced molten plastic has somewhere to go instead of oozing into the thread.
- **Wall thickness:** **≥2 mm** of plastic around the insert, and give the slicer 3–4 perimeters here — the knurl grips walls, not sparse infill.
- **Chamfer:** a small entry chamfer self-centers the insert and swallows the witness ring of displaced plastic.

## Installing without wrecking it

Use a **soldering iron with an insert tip** (Ruthex and others sell tip sets; a conical tip works for M3) at roughly **240 °C** for PLA/PETG. Rest the insert in the chamfer, let the iron's weight sink it slowly and *straight* — the moment it starts moving, gravity is enough force. Stop flush or 0.1 mm proud, remove the iron vertically, and let the boss cool undisturbed so the plastic solidifies around the knurl; pressing a screw in while soft pulls the insert back out. A flat plate laid over the insert as it cools guarantees flushness. Crooked insert = crooked screw = cracked boss, and reheating to straighten is never as strong.

## What the pull-out data says

CNC Kitchen's tests in PLA are the reference numbers. In the cheap-vs-expensive comparison, an M3 **Ruthex** insert held **~181 kgf (~1780 N)** before the boss failed, generic eBay knurled inserts ~157 kgf, and cheap straight-knurl injection-molding inserts only ~39 kgf (~380 N) — a 4× spread on the same screw size. The later diameter-sweep study measured **~1400 N** peak for properly sized holes. For torque, all decent inserts survived **3–4 Nm until the M3 bolt head sheared** — the insert outlives the fastener. Notably, a plain M3 screw self-tapped into a 2.7 mm hole managed ~142 kgf static pull-out too; its weakness is *cycling*, not the first pull.

## Materials

**PLA** installs easily (low melt temperature) but **creeps**: a boss under constant preload near a warm print bed or in a car slowly relaxes and the joint loosens. **PETG** and **ABS/ASA** hold preload better and tolerate the heat of a stalled install; ABS's higher glass transition also means the insert won't spin loose in a warm enclosure. For anything structural and warm — printer parts, automotive — PETG is the floor.

## The alternatives, honestly

| Method | Cycle life | Strength (M3-class) | Cost | Print complexity |
|---|---|---|---|---|
| Self-tapping screw in plastic | ~5–10 cycles | good until stripped | none | trivial hole |
| Printed thread (≥M8 coarse) | high at large pitch | scales with size | none | needs clean printing |
| Embedded/captive nut | very high | bolt-limited | cents | pocket + pause or slot |
| Heat-set insert | very high | boss-limited (~1.4–1.8 kN pull-out) | ~10–20 ¢ | hole + chamfer, install step |

Embedded hex nuts in printed pockets are the underrated middle: strength comparable to inserts, no iron required, at the cost of a pause-at-layer or a side slot.

## Modeling the boss

A reusable OpenSCAD boss (works standalone; also fine composed under BOSL2 `diff()`), matching the Ruthex numbers, ready to `difference()` from an [enclosure](/articles/cad-3dprint/2026-07-26-openscad-parametric-parts/):

```openscad
// Heat-set boss for Ruthex RX-M3x5.7: d_hole in CAD = 4.2, insert length 5.7
module insert_boss(h = 8, d_hole = 4.2, insert_l = 5.7, wall = 2.4) {
    difference() {
        cylinder(h = h, d = d_hole + 2*wall, $fn = 48);
        translate([0, 0, h - insert_l - 1])
            cylinder(h = insert_l + 1.01, d = d_hole, $fn = 48); // +1 mm melt relief
        translate([0, 0, h - 0.8])
            cylinder(h = 0.81, d1 = d_hole, d2 = d_hole + 1.2, $fn = 48); // chamfer
    }
}
```

In FreeCAD the same boss is a `PartDesign` pad with a pocketed hole — drive `d_hole` from a spreadsheet cell so switching insert vendors is a one-cell edit, exactly the pattern from the [spreadsheet-parametric workflow](/articles/cad-3dprint/2026-07-30-freecad-spreadsheet-parametric/).

**Try next:** print six copies of a test boss with CAD hole diameters from 3.9 to 4.4 mm in 0.1 mm steps, install a Ruthex RX-M3x5.7 in each, and find the one where the insert pre-seats with light resistance — that diameter is your printer's calibrated number; write it into your library file and stop guessing.
