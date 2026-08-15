---
title: "Dialing In New Filament: OrcaSlicer's Built-In Calibration Suite"
date: 2026-07-26
track: cad-3dprint
summary: "OrcaSlicer ships five calibration tests that turn a mystery spool into a known quantity — temperature, flow ratio, pressure advance, max volumetric speed and retraction, ordered so each test assumes the parameters ahead of it are fixed, and each writing straight back into the filament profile."
reading_time: 6
tags: [orcaslicer, 3d-printing, calibration, filament, slicer, flow-rate, pressure-advance]
sources:
  - title: "OrcaSlicer Wiki: Calibration"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/Calibration"
  - title: "OrcaSlicer Wiki: Flow Ratio Calibration"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/flow_ratio_calib"
  - title: "OrcaSlicer Wiki: Pressure Advance Calibration"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/pressure_advance_calib"
  - title: "OrcaSlicer: 3D Printer Calibration Features Deep Dive (Obico)"
    url: "https://www.obico.io/blog/orcaslicer-3d-printer-calibration/"
  - title: "Orca Slicer Calibration Suite: Temperature Towers, Flow Rate, Retraction, and PA Tuning (UAVMODEL)"
    url: "https://blog.uavmodel.com/orca-slicer-calibration-suite-temperature-towers-flow-rate-retraction-and-pa-tuning-2026-guide/"
---

**Gist.** A spool selected from a generic dropdown profile is not the material the profile describes: pigment load, moisture content and resin batch all vary, so extrusion behaviour drifts from the assumed values. OrcaSlicer's `Calibration` menu resolves the drift empirically — five printed tests, each yielding one number written into one field of the filament profile. The cost is machine time and material, and an ordering constraint: temperature first, retraction last, because each test assumes the parameters ahead of it are already fixed. The OrcaSlicer wiki's recommended sequence is **Temperature → Flow Rate → Pressure Advance → Retraction**, with max volumetric speed calibrated alongside; the sections below follow that grouping.

The ordering is what makes the suite work. Every later test is read visually, and every visual symptom it depends on can also be produced by a mis-set earlier parameter. A retraction tower printed at the wrong nozzle temperature measures temperature-induced ooze, not retraction distance. Running out of sequence therefore does not merely waste a print; it records a wrong number into the profile, and that wrong number then biases the tests run afterwards.

This is slicer-side calibration. All five values live in the filament profile and are emitted as G-code with each print, which distinguishes them from Klipper's firmware-side resonance tuning and pressure advance held in `printer.cfg`. Pressure advance is the one value both layers can hold, so a printer already carrying a tuned `pressure_advance` in `printer.cfg` should have the slicer's pressure advance override left disabled rather than set to a second value. Marlin-class boards, which hold no such per-filament value, take the whole set from the slicer.

## Temperature: the parameter every later test assumes

`Calibration > Temperature` prints a tower with one block per temperature step — the start and end temperatures are entered per material, stepping in 5 °C increments over the vendor's stated range for polylactic acid (PLA), and higher for polyethylene terephthalate glycol (PETG), thermoplastic polyurethane (TPU) and acrylonitrile butadiene styrene (ABS).

Three properties separate the blocks, and they fail in opposite directions. **Layer adhesion degrades at the cold end**: snapping a corner by hand produces a clean inter-layer break rather than a torn one. **Bridging quality** and **surface finish degrade at the hot end**, where the melt stays fluid too long and produces stringing and sagging overhangs. The acceptable window is therefore bounded on both sides, and when several adjacent blocks pass, the midpoint balances the two failure modes while the upper end buys additional flow headroom for fast printing.

The result is entered in **Filament Settings > Temperature > Nozzle**, separately for the first layer and remaining layers; the first layer is commonly run a few degrees hotter for bed adhesion.

## Max volumetric speed: the flow ceiling of the melt zone

Every filament has a ceiling on the volumetric rate, in mm³/s, that the melt zone can process. Beyond it the hotend cannot deliver molten material at the commanded rate and the print under-extrudes, **regardless of how fast the motion system can move** — the constraint is thermal, not kinematic.

`Calibration > Max Volumetric Speed` prints a ramp with configurable start speed, end speed and step; defaults are 5→20 mm³/s in steps of 0.5. The height at which under-extrusion or gaps first appear identifies the ceiling, read either by eye or from the Preview tab's flow colour scheme at that height:

```
max_volumetric_speed = start + (measured_height_mm × step)
```

The entered value should sit below the measured figure. The margin covers retraction moves and cornering, where **instantaneous flow briefly exceeds the steady-state rate** that the ramp measures. The field is **Filament Settings > Advanced > Max volumetric speed**.

## Pressure advance: three geometries, one number

Pressure advance compensates for lag in the melt zone at corners and speed changes. The failure modes are directional and diagnostic: **too low leaves bulging outer corners**, because pressure built up during the fast segment continues to extrude into the slow one; **too high leaves gaps immediately after corners**, because the compensating retraction overshoots.

`Calibration > Pressure Advance` offers three geometries:

- **Line** — parallel lines at increasing pressure advance; the most uniform-width line wins. It is read entirely off the first layer, so **first-layer squareness dominates the reading** and a fresh bed mesh matters.
- **Pattern** — small handled blocks, each printed at a fixed value baked in as custom G-code per block. More precise than the line test, and several filaments can be batched onto one plate.
- **Tower** — one object with the value increasing by height, so the reading does not depend on first-layer quality. The height at which corners are cleanest solves `pa = pa_start + (pa_step × height_mm)`.

The result is a **filament-level** property, not a printer-level one, and belongs in the material profile with the pressure advance checkbox enabled in Filament Settings. This is the test with genuine overlap with Klipper: a `pressure_advance` already tuned in `printer.cfg` combined with a non-zero slicer value double-compensates, producing the over-tuned symptom (post-corner gaps) even though neither value alone is too high.

## Flow ratio: two passes over a scaling factor

Flow ratio scales every extrusion move. **Too low starves the print** — gaps between walls, weak layer bonds, visible under-extrusion. **Too high overstuffs it** — rough or blobby top surfaces and oversized dimensions. `Calibration > Flow Ratio` narrows the value in two passes:

- **Pass 1** slices a coarse sweep of blocks, modifiers −20 to +5 in steps of 5, each applying that percentage modifier to the current ratio. The block with the smoothest, most consistent top surface identifies the modifier.
- **Pass 2** repeats with a finer sweep, modifiers −9 to 0 in steps of 1.

After each pass the ratio is updated by

```
new_ratio = old_ratio × (100 + modifier) / 100
```

which is the general form `new_ratio = old_ratio × (measured / target)`: the modifier expresses the winning block's flow as a percentage of the 100 % target. Starting from a ratio of 0.98 with a Pass 1 winner at +5 gives `0.98 × 105 / 100 = 1.029`, and Pass 2 starts from 1.029. **The passes are multiplicative, not additive** — applying Pass 2's modifier to the original ratio discards Pass 1's correction.

The final value goes in **Filament Settings > Advanced > Flow ratio**. Calipers provide an independent check: a single-wall test object should measure close to the configured extrusion width, and a systematic deviation scales the ratio by measured/target.

## Retraction: last, because its symptom is not specific

Stringing is the symptom of retraction distance, but it is also a symptom of excessive nozzle temperature and of excessive flow — which is why the test runs after both are fixed. `Calibration > Retraction Test` prints a tower with two posts and a retraction distance increasing with height by a configurable step. The height with the least visible stringing between the posts back-solves the distance. Usable ranges differ by extruder topology: **direct-drive extruders settle at distances of roughly a millimetre or two, Bowden setups at several millimetres**, the difference being the length of compliant filament between drive gear and nozzle.

The value is entered under **Filament Settings > Filament Overrides**, with the retraction length override enabled so this filament ignores the printer's global default.

## Test-to-symptom reference

| Test | Symptom it fixes | Where the value goes |
|---|---|---|
| Temperature tower | Weak layers, stringing, bad bridging/overhangs | Filament Settings > Temperature > Nozzle |
| Max volumetric speed | Under-extrusion at high print speed | Filament Settings > Advanced > Max volumetric speed |
| Pressure advance | Bulging corners (too low) / gaps after corners (too high) | Filament Settings > Pressure Advance (enable checkbox) |
| Flow ratio | Gaps/weak bonds (under) or rough, oversized walls (over) | Filament Settings > Advanced > Flow ratio |
| Retraction | Stringing between features | Filament Settings > Filament Overrides > Retraction length |

The cost is incurred once per filament brand and colour rather than per print, provided the finished profile is saved and reused. Re-running is warranted after a batch change (new colour, new moisture exposure) or a hardware change (new nozzle diameter, new hotend).

## Pitfalls

- **Retraction tuned before temperature.** Stringing persists at every distance on the tower, because the ooze is thermal and no retraction distance suppresses it.
- **A non-zero slicer pressure advance on a Klipper printer with `pressure_advance` set in `printer.cfg`.** Gaps appear after corners even though each value alone is reasonable; the two compensations sum.
- **Entering the measured max volumetric speed without a margin below it.** Prints look correct in straight runs and under-extrude at corners and after retractions, where instantaneous flow exceeds the steady-state rate.
- **Applying the Pass 2 flow modifier to the pre-Pass-1 ratio.** The final ratio is off by the Pass 1 correction, and single-wall measurements stay systematically away from the configured extrusion width.
- **Reading the line-geometry pressure advance test off a bed with a stale mesh.** Line width varies with first-layer height rather than with pressure advance, so the selected value reflects bed topology.
- **Saving pressure advance or flow ratio into the printer profile.** The values then apply to every filament, and the next spool inherits a correction measured on a different material.
