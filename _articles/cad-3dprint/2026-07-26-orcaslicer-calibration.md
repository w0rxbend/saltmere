---
title: "Dialing In New Filament: OrcaSlicer's Built-In Calibration Suite"
date: 2026-07-26
track: cad-3dprint
summary: "OrcaSlicer ships five calibration tests that turn a mystery spool into a known quantity — temperature, max volumetric speed, pressure advance, flow ratio, and retraction, run in that order, each writing straight back into the filament profile."
reading_time: 5
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

A new spool of filament isn't the same material as the profile you picked from the dropdown. Different pigment loads, different moisture content, different resin batch from the same brand — the generic "Generic PLA" profile is a starting guess, not a fit. OrcaSlicer's `Calibration` menu exists to close that gap: five tests, each producing one number, each number going into one field on the filament profile. Run them out of order and you'll chase symptoms that a later test would have fixed for free — the wiki's recommended sequence is **Temperature → Max Volumetric Speed → Pressure Advance → Flow Ratio → Retraction**, and it's worth following literally, because each test assumes the ones before it are already dialed.

This is slicer-side calibration: everything here lives in the filament profile and ships as G-code with every print. It's a different layer from Klipper's firmware-side resonance tuning and pressure advance (covered separately) — Klipper users who already run `SHAPER_CALIBRATE` and a live-tuned `pressure_advance` in `printer.cfg` can skip Orca's PA test and just leave the slicer's PA field at zero, since the firmware value takes precedence. Everyone else, including anyone printing from a Marlin-class board, gets value baked in per-filament from Orca's own tests.

## Temperature: the foundation everything else assumes

`Calibration > Temperature` prints a tower with a block per temperature step (defaults roughly 190–220°C for PLA in 5°C increments, higher and wider for PETG/TPU/ABS). Judge each block on three things: layer adhesion (try snapping a corner off by hand — clean break means too cold), bridging quality, and surface finish (too hot shows stringing and sagging overhangs). If a range of blocks all look acceptable, pick the middle for balanced quality, or the higher end if you're printing fast and need the extra flow. Enter the result in **Filament Settings > Temperature > Nozzle** (first layer and other layers separately — first layer usually runs a few degrees hotter for bed adhesion).

## Max volumetric speed: how fast this filament can actually flow

This test exists because every filament has a ceiling on mm³/s the melt zone can process before the hotend can't keep up — push past it and you get under-extrusion regardless of how fast your motors can move. `Calibration > Max Volumetric Speed` prints a ramp with configurable start/end speed and step (defaults 5→20 mm³/s, step 0.5). Read the result either visually (find the height where under-extrusion or gaps first appear) or precisely via the Preview tab's flow color scheme at that height:

```
max_volumetric_speed = start + (measured_height_mm × step)
```

Take the number and shave 10–20% off before entering it — that margin covers retraction moves and cornering, where instantaneous flow briefly exceeds the steady-state print speed. The value goes in **Filament Settings > Advanced > Max volumetric speed**.

## Pressure advance: three ways to the same number

Pressure advance compensates for melt-zone lag at corners and speed changes — under-tuned, you get bulging outer corners; over-tuned, you get gaps right after them. OrcaSlicer offers three test geometries under `Calibration > Pressure Advance`:

- **Line** — fastest, prints parallel lines at increasing PA; pick the most uniform-width line. Depends heavily on first-layer squareness, so a fresh bed mesh matters.
- **Pattern** — small handled blocks, each printed at a fixed PA value baked in as custom G-code per block; more precise than the line test, supports batching several filaments on one plate.
- **Tower** — a single print with PA increasing by height, immune to first-layer inconsistency. Read the height where corners are cleanest and solve `pa = pa_start + (pa_step × height_mm)`.

Whichever method, the result is a filament-level value, not a printer-level one — save it in the **material profile**, with the pressure advance checkbox enabled in Filament Settings, not in the printer profile. This is the one test with real Klipper overlap: if you already tune `pressure_advance` in `printer.cfg`, leave Orca's field at 0 so you're not double-compensating.

## Flow ratio: the two-pass method

Flow ratio scales every extrusion move — too low starves the print (gaps between walls, weak layer bonds, visible under-extrusion); too high overstuffs it (rough, blobby surfaces, oversized dimensions, elephant's foot). `Calibration > Flow Ratio` runs in two passes:

- **Pass 1** slices nine blocks, each nudging flow by a different percentage modifier. Print, find the block with the smoothest, most consistent top surface, and note its modifier.
- **Pass 2** re-runs around that result with a finer sweep (roughly ten blocks, modifiers -9 to 0) for a second, tighter pass.

After each pass, update the ratio with:

```
new_ratio = old_ratio × (100 + modifier) / 100
```

which is the general form of `new_ratio = old_ratio × (measured / target)` — the modifier expresses the winning block's flow as a percentage of the 100% target. Example: starting ratio 0.98, Pass 1 winner is the +5 block → `0.98 × 105 / 100 = 1.029`. Run Pass 2 starting from 1.029 for the final value. Enter it in **Filament Settings > Advanced > Flow ratio**. Verify with calipers afterward: a 0.4 mm nozzle at 0.45 mm extrusion width should print single walls at roughly 0.43–0.47 mm.

## Retraction: last, because it depends on everything above

Stringing looks like a retraction problem but is frequently a temperature or flow problem wearing a disguise — hence running this test last. `Calibration > Retraction Test` prints a tower with two posts and a retraction distance that increases with height (roughly 0.2 mm added per 1 mm of tower height). Find the height with the least visible stringing between posts and back-solve the corresponding distance. Typical usable ranges: 0.2–1.4 mm for direct drive, 2–7 mm for Bowden. Enter the result under **Filament Settings > Filament Overrides**, enabling the retraction length override so this filament ignores the printer's global default.

## Test-to-symptom reference

| Test | Symptom it fixes | Where the value goes |
|---|---|---|
| Temperature tower | Weak layers, stringing, bad bridging/overhangs | Filament Settings > Temperature > Nozzle |
| Max volumetric speed | Under-extrusion at high print speed | Filament Settings > Advanced > Max volumetric speed |
| Pressure advance | Bulging corners (too low) / gaps after corners (too high) | Filament Settings > Pressure Advance (enable checkbox) |
| Flow ratio | Gaps/weak bonds (under) or rough, oversized walls (over) | Filament Settings > Advanced > Flow ratio |
| Retraction | Stringing between features | Filament Settings > Filament Overrides > Retraction length |

Each of these is a one-time cost per filament brand/color, not per print — save the finished profile and reuse it. Re-run only after a batch change (new color, new moisture exposure) or a hardware change (new nozzle diameter, new hotend).

**Try next:** pick one spool you've never formally calibrated, run all five tests in order in a single evening, save it as a distinct filament profile, and diff the resulting numbers against the generic profile you started from.
