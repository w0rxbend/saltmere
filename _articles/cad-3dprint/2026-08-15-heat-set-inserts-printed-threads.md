---
title: "Heat-Set Inserts vs Printed Threads: Fastening Parts That Come Apart"
date: 2026-08-15
track: cad-3dprint
summary: "A printed M3 thread strips after a handful of cycles; a brass heat-set insert in the same boss survives hundreds and pulls out at well over 1000 N in PLA. Where printed threads genuinely win (coarse, large, BOSL2-generated), how to size the boss — ~4.0 mm hole for a Ruthex RX-M3x5.7, wall over 2 mm, depth one insert-length plus 1 mm — and what CNC Kitchen's pull-out testing measured."
reading_time: 6
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

**Gist.** A screw driven repeatedly into printed plastic — bare hole or printed M3 thread — strips, because every cycle shaves the same soft flanks and fused-deposition-modelling (FDM) layer lines give those flanks little shear area. The durable answers are **coarse, large-diameter printed threads** where the pitch is large relative to the nozzle, and **knurled brass heat-set inserts** everywhere small, which melt into an undersized boss so the plastic solidifies inside the knurl and locks the bushing mechanically. The cost is geometric discipline plus a manual install step: the boss must be sized to the insert within roughly a tenth of a millimetre, and an insert seated crooked or loaded before the plastic cools is weaker than no insert at all.

## Where printed threads win

FDM resolves a thread form once the pitch is large relative to the extrusion width: from about **M8 upward**, and for any custom coarse or trapezoidal profile, printed threads are strong, cost nothing, and require no hardware. Jar-style lids, sensor-pod collars and knurled thumbwheels belong in this class. In OpenSCAD, [BOSL2's](/articles/cad-3dprint/2026-07-31-bosl2-openscad-library/) `threading.scad` generates matching internal and external pairs with a `$slop` clearance parameter, and ships dedicated pipe-thread (NPT) modules — profiles that are coarse by definition. Threads printed about the vertical axis resolve best; a small lead-in chamfer and a break-in turn or two are expected.

The failure case is the small metric sizes under repeated use. **An M3 printed thread has flanks a fraction of a layer tall and strips at a torque well below what an M3 bolt is normally tightened to**, and it degrades further with every cycle. No published test in the cited sources puts a number on the stripping torque.

## The heat-set boss, sized correctly

A **heat-set insert** is a knurled brass bushing melted into an undersized hole. The plastic does not bond chemically to the brass; it flows into the knurl and, on solidifying, **interlocks**. Because the joint is mechanical rather than adhesive, the boss geometry carries the whole load path.

- **Hole diameter:** taken from the insert specification. For the **Ruthex RX-M3x5.7** (M3, 5.7 mm long) the manufacturer's figure is a **4.0 mm** hole. CNC Kitchen's diameter sweep found peak pull-out at roughly **4.0–4.1 mm actual** so the figure modelled in computer-aided design (CAD) has to be enlarged by whatever the printer's holes come out undersized by — commonly a couple of tenths of a millimetre, which puts the CAD figure near **4.2 mm**. The acceptance criterion is tactile rather than numeric: the insert pre-seats cold with slight resistance. Oversizing past the peak loses strength, and the loss accelerates as the knurl stops biting.
- **Hole depth:** insert length **plus at least 1 mm of clear bore below**, so displaced molten plastic has volume to occupy instead of being forced up into the knurl or out of the mouth.
- **Wall thickness:** **≥2 mm of plastic around the insert**, printed with 3–4 perimeters. The knurl transfers load into solid wall, not into sparse infill.
- **Chamfer:** a small entry chamfer self-centres the insert and accommodates the witness ring of displaced plastic.

The two dimensions interact. An undersized hole with adequate depth still installs, because the excess plastic has somewhere to go; a correctly sized hole with no relief below pushes melt upward and leaves the insert sitting proud, which is then corrected by pressing harder — and pressing harder while the boss is soft is the mechanism that splits walls.

## Installation as a state machine

The install has three states and only the last one is load-bearing.

1. **Heating.** A soldering iron with an insert tip (conical works for M3) set somewhat above the printing temperature of the polymer — polylactic acid (PLA) and glycol-modified polyethylene terephthalate (PETG) both install in the region of **220–250 °C**. The insert rests in the chamfer; the iron's own weight supplies the force. Once the insert begins to move, no additional push is required.
2. **Seating.** The insert descends straight. It is stopped flush or 0.1 mm proud, and the iron is withdrawn vertically so it does not tilt the bushing on release. A flat plate laid over the insert during withdrawal enforces flushness.
3. **Cooling.** The boss must cool undisturbed. **Driving a screw while the plastic is still soft extracts the insert**, because the knurl is not yet keyed into anything solid.

The dominant defect is angular. A crooked insert produces a crooked screw axis, which loads the boss wall in bending rather than the knurl in shear, and the boss cracks. Reheating to straighten a seated insert does not restore the original strength — the knurl re-melts a cavity it has already deformed.

## What the pull-out data measured

CNC Kitchen's PLA tests are the reference figures, and the spread between insert types is larger than the spread between design choices.

| Specimen | Pull-out |
|---|---|
| M3 Ruthex insert | ~181 kgf (~1780 N), boss failed |
| Generic knurled eBay insert | ~157 kgf |
| Cheap straight-knurl injection-moulding insert | ~39 kgf (~380 N) |
| M3 screw self-tapped into a plain printed hole | ~142 kgf |

That is roughly a **4× spread at one screw size**, driven by knurl geometry rather than by brass quality: the straight-knurl inserts intended for injection moulding have no barbs opposing axial extraction. The later diameter-sweep study measured **~1400 N** peak for properly sized holes — a different specimen and test setup, so the two numbers bound a range rather than contradicting each other.

Under torque, decent inserts held **until the M3 bolt itself failed rather than the insert**: the insert outlives the fastener, which is the property a printed thread lacks. The self-tapped screw's static pull-out is respectable, and its weakness is **cycling**, not the first pull — a distinction a single-pull test cannot show.

## Materials

**PLA** installs easily owing to its low melt temperature but **creeps**: a boss held under constant preload near a warm print bed, or in a parked car, relaxes over time and the joint loosens without anything visibly failing. **PETG** and **acrylonitrile butadiene styrene / acrylonitrile styrene acrylate (ABS/ASA)** hold preload better and tolerate a stalled install where the iron dwells too long. ABS's higher glass-transition temperature also keeps the insert from turning loose in a warm enclosure. For structural joints that get warm — printer parts, automotive — PETG is the floor.

## The alternatives

| Method | Cycle life | Strength (M3-class) | Cost | Print complexity |
|---|---|---|---|---|
| Self-tapping screw in plastic | a handful of cycles | good until stripped | none | plain hole |
| Printed thread (≥M8 coarse) | high at large pitch | scales with size | none | needs clean printing |
| Embedded/captive nut | very high | bolt-limited | cents | pocket + pause or slot |
| Heat-set insert | very high | boss-limited (~1.4–1.8 kN pull-out) | ~10–20 ¢ | hole + chamfer, install step |

Embedded hex nuts in printed pockets occupy the underrated middle: strength comparable to inserts and no iron required, at the cost of a pause-at-layer or a side-entry slot.

## Modelling the boss

A reusable OpenSCAD boss, matching the Ruthex figures, suitable for `difference()` against an [enclosure](/articles/cad-3dprint/2026-07-26-openscad-parametric-parts/). It works standalone and composes under BOSL2 `diff()`.

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

The `1.01` and `0.81` overshoots exist to avoid coincident faces at the boss top, which OpenSCAD's constructive-solid-geometry evaluation renders as zero-thickness artefacts. `wall = 2.4` clears the ≥2 mm minimum with margin for a printer whose holes come out undersized.

In FreeCAD the same boss is a `PartDesign` pad with a pocketed hole; driving `d_hole` from a spreadsheet cell makes an insert-vendor change a one-cell edit, following the [spreadsheet-parametric workflow](/articles/cad-3dprint/2026-07-30-freecad-spreadsheet-parametric/).

Calibration is empirical and cheap: print six test bosses with CAD hole diameters from 3.9 to 4.4 mm in 0.1 mm steps, install a Ruthex RX-M3x5.7 in each, and record the diameter at which the insert pre-seats with light resistance. That value is the printer's calibrated number and belongs in the library file.

## Pitfalls

- **The insert sinks fast and tilts.** The iron was pushed rather than allowed to fall; once the plastic softens, applied force translates into angular error, and the resulting screw axis loads the boss wall in bending.
- **A screw driven immediately after install pulls the insert straight back out.** The plastic has not solidified around the knurl, so nothing opposes axial extraction.
- **The insert sits proud and refuses to go flush.** The bore below is shorter than insert length plus 1 mm, so displaced melt has no relief volume.
- **The boss cracks along a layer line during install.** Wall thickness below 2 mm, or perimeter count too low, so hoop stress from the expanding melt is carried by infill.
- **The insert spins in its boss under torque after weeks in a warm enclosure.** PLA above its softening range creeps under preload; the knurl interlock relaxes.
- **Cheap injection-moulding inserts pull out at a fraction of the rated load.** Straight knurls provide no barb opposing axial extraction — CNC Kitchen measured ~39 kgf against ~181 kgf for a knurled Ruthex insert.
- **A hole modelled at the datasheet 4.0 mm prints undersized and the insert will not pre-seat.** Printed holes come out smaller than modelled, so the datasheet figure is a target for the printed hole, not for the CAD sketch.
- **An M3 printed thread strips at low torque.** Its flanks are a fraction of a layer tall; below M8 the thread form is not resolved well enough to carry repeated cycles.
