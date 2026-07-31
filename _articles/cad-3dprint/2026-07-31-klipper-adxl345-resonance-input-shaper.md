---
title: "Measuring your printer's resonances with an ADXL345 and letting Klipper tune input shaping"
date: 2026-07-31
track: cad-3dprint
summary: "Ghosting and ringing are your printer's frame ringing like a bell at its resonant frequency. Instead of guessing input-shaper values, bolt a $5 ADXL345 accelerometer to the toolhead, let Klipper excite each axis and record the response, and have it compute the shaper type and frequency that cancel the ringing. Here's the wiring, config, and command flow."
reading_time: 5
tags: [klipper, adxl345, input-shaper, resonance, 3d-printing, calibration]
sources:
  - title: "Klipper — Measuring Resonances (official docs)"
    url: "https://www.klipper3d.org/Measuring_Resonances.html"
  - title: "Klipper — Resonance Compensation (input shaper theory)"
    url: "https://www.klipper3d.org/Resonance_Compensation.html"
  - title: "Klipper adxl345 / resonance_tester config reference"
    url: "https://www.klipper3d.org/Config_Reference.html#resonance_tester"
  - title: "Analog Devices ADXL345 datasheet (SPI/I2C 3-axis accelerometer)"
    url: "https://www.analog.com/media/en/technical-documentation/data-sheets/ADXL345.pdf"
  - title: "Klipper G-Codes — ACCELEROMETER_QUERY / TEST_RESONANCES / SHAPER_CALIBRATE"
    url: "https://www.klipper3d.org/G-Codes.html#resonance_tester"
---

Print a cube fast and look at the wall just after each corner: faint repeating echoes of the corner, fading out. That's **ringing** (a.k.a. ghosting), and it isn't a slicer problem — it's physics. When the toolhead changes direction sharply it hammers the frame, and the frame rings at its natural resonant frequency like a struck bell, dragging the nozzle a few microns back and forth. Klipper's **input shaper** cancels this by shaping the acceleration commands so they don't excite that frequency. But it can only cancel a frequency it *knows*, and every printer's is different. An ADXL345 accelerometer lets you measure it instead of guessing.

## Wiring the sensor

The **ADXL345** is a 3-axis digital accelerometer that speaks SPI (preferred for Klipper's high sample rate) and costs a few dollars on a breakout. You mount it rigidly to the toolhead — a printed bracket that clamps to the hotend or carriage — because it must feel exactly what the nozzle feels. Wire its SPI pins to your MCU (often a spare header on the main board, or a Raspberry Pi's SPI). Then declare it and a `resonance_tester` that knows where to run the test:

```ini
[adxl345]
cs_pin: rpi:None            # SPI chip-select (example: Pi host SPI)
# spi_bus / pins depend on where you wired it

[resonance_tester]
accel_chip: adxl345
probe_points:
    117, 117, 20           # X, Y, Z near the center of the bed
```

`probe_points` is where the toolhead sits while Klipper shakes it — pick a spot near the middle of the build area at a modest Z.

## Verify the sensor before trusting it

Two commands sanity-check the wiring first. Skipping these is how people spend an evening tuning against garbage data:

```
ACCELEROMETER_QUERY        # reads current acceleration on all 3 axes
```

At rest this should report roughly `9800 mm/s^2` (≈1 g) on the vertical axis and near zero on the others — that's gravity, and seeing it proves the sensor is alive and oriented sanely. Then measure the noise floor:

```
MEASURE_AXES_NOISE         # baseline sensor noise per axis
```

Low, stable numbers here mean a clean signal; wildly high noise usually means a loose sensor or bad wiring, and you fix that *now*, not after a confusing calibration.

## Excite each axis and let Klipper do the math

The measurement itself: Klipper vibrates the toolhead through a sweep of frequencies along one axis and records how hard the frame responds at each — a frequency-response curve whose peak is your resonance.

```
TEST_RESONANCES AXIS=X     # sweeps X, writes /tmp/resonances_x_*.csv
TEST_RESONANCES AXIS=Y     # sweeps Y
```

You can plot those CSVs with Klipper's bundled `scripts/calibrate_shaper.py` to *see* the peaks, but the one-shot path is:

```
SHAPER_CALIBRATE           # runs both axes and picks shaper + frequency
```

`SHAPER_CALIBRATE` sweeps X and Y, evaluates every shaper type against your measured curve, and prints a recommendation — something like:

```
Recommended shaper_type_x = mzv,   shaper_freq_x = 47.8 Hz
Recommended shaper_type_y = ei,    shaper_freq_y = 41.2 Hz
```

It also tells you the **max acceleration** at which each shaper still works, which is the number that actually lets you print faster without the ringing coming back. Run `SAVE_CONFIG` to write the result into `printer.cfg`:

```ini
[input_shaper]
shaper_type_x: mzv
shaper_freq_x: 47.8
shaper_type_y: ei
shaper_freq_y: 41.2
```

## Why the *type* matters, not just the frequency

The shaper types trade off differently. `zv` is the mildest (fast, least smoothing, but less robust if your resonance drifts); `mzv` and `ei` are more robust to frequency error at the cost of a little extra smoothing on fine detail. `SHAPER_CALIBRATE` picks based on how much vibration each leaves versus how much it smooths features, but if you later see corners looking soft you can nudge toward a lower-smoothing type, and if ghosting creeps back after a belt change, just re-run the test — resonance shifts with belt tension, toolhead mass, and frame changes, so it's a re-measure, not a one-time set.

**Try next:** print a "ringing tower" test model at your normal speed *before* calibrating and keep it. Then run `SHAPER_CALIBRATE`, `SAVE_CONFIG`, and print the identical tower again. Set the two side by side under a raking light — the echoes past each layer's protrusions should be visibly gone. That before/after is the most convincing five minutes in printer tuning, and it's driven entirely by a $5 sensor and a frequency you measured instead of guessed.
