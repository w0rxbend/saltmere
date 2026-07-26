---
title: "Klipper tuning: killing ringing with input shaping, killing blobs with pressure advance"
date: 2026-07-26
track: cad-3dprint
summary: "Ringing and corner blobbing aren't slicer settings you can dial away — they're mechanical resonance and extruder pressure lag. Klipper fixes both in firmware: accelerometer-driven input shaping cancels ringing, and pressure advance pre-compensates nozzle pressure at corners. Concrete configs and tuning commands included."
reading_time: 5
tags: [klipper, 3d-printing, input-shaping, pressure-advance, firmware, calibration]
sources:
  - title: "Measuring Resonances (Klipper docs)"
    url: "https://www.klipper3d.org/Measuring_Resonances.html"
  - title: "Resonance Compensation (Klipper docs)"
    url: "https://www.klipper3d.org/Resonance_Compensation.html"
  - title: "Pressure Advance (Klipper docs)"
    url: "https://www.klipper3d.org/Pressure_Advance.html"
  - title: "G-Codes reference (Klipper docs)"
    url: "https://www.klipper3d.org/G-Codes.html"
  - title: "Tower Method (Ellis' Print Tuning Guide)"
    url: "https://ellis3dp.com/Print-Tuning-Guide/articles/pressure_linear_advance/tower_method.html"
---

Print a fast cube in vase mode and look at the walls near a corner: faint parallel ripples, like an echo of the edge repeating a few millimeters into the surface. That's ringing (also called ghosting). It's not a slicer bug and it's not layer adhesion — it's the toolhead physically vibrating after a sudden acceleration change, and the nozzle dutifully extruding plastic while the frame oscillates. Klipper attacks this in firmware with two independent, complementary features: **input shaping** (cancels the vibration itself) and **pressure advance** (cancels a separate defect — extruder pressure lag at direction changes, which shows up as corner blobs or gaps). Neither is a slicer setting. Both are measured and configured on the printer.

## Ringing: a mechanical resonance problem

Every axis is a mass (toolhead, gantry, bed) on a spring (belts, frame rigidity). Command a sharp velocity change — the end of a fast infill line, a perimeter corner — and you excite that spring-mass system at its natural frequency. It rings for a few tens of milliseconds while still moving and extruding, imprinting the vibration onto the print surface. Higher speed and acceleration make it worse because direction changes get sharper and more frequent.

The real fix is mechanical: stiffen the frame, tension belts correctly, reduce moving mass. Klipper's own docs are explicit that input shaping mitigates *remaining* resonance — it's not a replacement for a rigid machine. But mechanical fixes have limits, and that's where input shaping earns its keep.

## Input shaping: cancel vibration with math, not mass

Input shaping is an open-loop control technique: instead of sending the ideal step pulse train, Klipper convolves the motion command with a short *shaper* — a small sequence of impulses spaced and scaled so the vibration each impulse induces destructively interferes with the vibration from the others. Net result: the toolhead still gets where it needs to go, but the resonant ringing at the target frequency is suppressed.

Different shapers trade off vibration cancellation against added smoothing (softened corners, since the shaper spreads a sharp move over a short window):

| Shaper | Duration | Vibration reduction (5% tolerance) | Notes |
|---|---|---|---|
| ZV | 0.5 / freq | ~5% tolerance band | Shortest, least smoothing, but sensitive if the real frequency drifts |
| MZV | 0.75 / freq | ~4% tolerance band | Good general-purpose default; low smoothing |
| ZVD | 1 / freq | ~15% tolerance band | More robust than ZV, more smoothing |
| EI | 1 / freq | ~20% tolerance band | Tolerates frequency drift well; common on bed-slingers |
| 2HUMP_EI | 1.5 / freq | -40…+45% | Handles printers with two resonance peaks |
| 3HUMP_EI | 2 / freq | -50…+60% | Most robust to multiple/wide resonances, most smoothing |

Rule of thumb: MZV for well-built CoreXY machines with a single clean resonance peak, EI or 2HUMP_EI for bed-slingers or frames with messier resonance spectra.

## Measuring resonances with an accelerometer

Guessing a shaper frequency doesn't work reliably — you need the printer's actual resonance peak. Klipper drives this with an ADXL345 accelerometer (an MPU-9250 or LIS2DW12 also work), wired to a spare MCU or a Raspberry Pi's SPI pins.

```
[mcu adxl]
serial: /dev/serial/by-id/usb-Klipper_rp2040_<serial>

[adxl345]
cs_pin: adxl:gpio1
spi_bus: spi0a
axes_map: x,z,y

[resonance_tester]
accel_chip: adxl345
probe_points:
    117, 117, 20
```

`probe_points` should be roughly bed center, at a safe Z height. Mount the accelerometer rigidly to the toolhead — a printed clip, screws tight, no rattle. Then, from the console:

```
ACCELEROMETER_QUERY
MEASURE_AXES_NOISE
TEST_RESONANCES AXIS=X
TEST_RESONANCES AXIS=Y
```

`ACCELEROMETER_QUERY` confirms the chip is wired and live. `TEST_RESONANCES` sweeps frequencies on the given axis, dumping raw acceleration data to `/tmp`. Feed it to the bundled analysis script:

```
~/klipper/scripts/calibrate_shaper.py /tmp/resonances_x_*.csv -o /tmp/shaper_x.png
```

The output recommends a shaper type and frequency directly, or you can skip the manual CSV step entirely and run `SHAPER_CALIBRATE AXIS=X` (and `AXIS=Y`), which does the sweep, analysis, and recommendation in one command and can even `SAVE_CONFIG` the result.

No accelerometer? A print-based ringing tower (a tall thin tower with a bump feature at increasing Z, sliced at increasing speed) is a coarse fallback: print it, find the height where ringing first appears, and back-calculate an approximate frequency from print speed and bump spacing. It's a rough substitute, not an equivalent, for real accelerometer data.

## Configuring [input_shaper]

Once you have frequencies (and ideally a suggested shaper type per axis), set them in `printer.cfg`:

```
[input_shaper]
shaper_freq_x: 52.6
shaper_freq_y: 38.2
shaper_type_x: mzv
shaper_type_y: ei
```

`shaper_type` applies to both axes if the per-axis variant isn't given. Restart the firmware (`RESTART`) to apply. You can also adjust shaping live without a restart, useful while comparing shapers on a test print:

```
SET_INPUT_SHAPER SHAPER_TYPE=EI SHAPER_FREQ_X=52.6 SHAPER_FREQ_Y=38.2
```

## Pressure advance: fixing corner blobs and gaps, not ringing

Input shaping cancels vibration; it does nothing for a separate effect. The nozzle and melt zone behave like a compressed spring: extruding builds melt pressure that keeps pushing plastic out for a moment after the extruder stops, and takes a moment to rebuild when extrusion resumes. At a sharp corner the toolhead decelerates and re-accelerates while flow rate changes abruptly, so uncompensated you get a blob (excess pressure discharging into the slow corner) or, if overcorrected, a gap.

Pressure advance predicts this and shifts extruder position slightly ahead of the corner, proportional to instantaneous flow rate, so melt pressure is already correct when the toolhead needs it.

Tune it live with a tuning tower macro rather than guessing values print-by-print:

```
TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE PARAMETER=ADVANCE START=0 FACTOR=.0025
```

(Use `FACTOR=.025` for Bowden, which needs larger values than direct drive.) Slice a thin-walled tower — one perimeter, no infill, external perimeter speed ~120 mm/s, minimum layer time disabled — and issue the macro right after the print starts; Klipper increments `pressure_advance` as height increases. Inspect the tower: bulging corners mean pressure advance is still too low at that height, clean corners with visible gaps mean it overshot. Measure the height where corners look cleanest and multiply by `FACTOR` for your tuned value:

```
SET_PRESSURE_ADVANCE ADVANCE=0.045
```

```
[extruder]
pressure_advance: 0.045
pressure_advance_smooth_time: 0.040
```

`pressure_advance_smooth_time` (default 0.040s) smooths the advance signal itself; leave it at default unless you're chasing extruder-motor noise at high pressure advance values.

## Symptom-to-fix quick reference

| Symptom | Cause | Fix |
|---|---|---|
| Rippled "echo" pattern on walls, worse at high speed | Frame/gantry resonance | Input shaping (`TEST_RESONANCES` + `[input_shaper]`) |
| Blobs/bulges at outer corners | Excess melt pressure discharging at direction change | Raise `pressure_advance` |
| Gaps or thin walls right after corners | Melt pressure overcorrected, briefly under-extruding | Lower `pressure_advance` |
| Ringing persists after shaper tuning | Mechanical looseness (belts, frame, loose pulley) exceeds what shaping can cancel | Fix mechanics first, re-run `SHAPER_CALIBRATE` |

Input shaping and pressure advance solve different physics and are tuned independently — don't chase one to fix symptoms of the other.

**Try next:** run `SHAPER_CALIBRATE AXIS=X` and `AXIS=Y` on your own printer, save the recommended frequencies to `[input_shaper]`, then print the same test file before and after — compare wall ringing at a fixed 100 mm/s perimeter speed.
