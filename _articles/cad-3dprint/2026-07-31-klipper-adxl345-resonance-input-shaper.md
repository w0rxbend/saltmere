---
title: "Measuring printer resonances with an ADXL345 and calibrating Klipper input shaping"
date: 2026-07-31
track: cad-3dprint
summary: "Ringing on printed walls is the frame oscillating at its natural frequency after each direction change. Klipper's input shaper cancels that oscillation by splitting each acceleration command into timed impulses, but only at a frequency it has been told. An ADXL345 accelerometer mounted on the toolhead supplies that frequency by measurement, at the cost of the smoothing the shaper imposes on fine detail."
reading_time: 6
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

**Gist.** A sharp direction change delivers an impulse to the printer frame, which then oscillates at its own natural frequency and drags the nozzle back and forth, leaving the repeating echoes known as ringing or ghosting. Klipper's **input shaper** suppresses the oscillation by replacing each commanded acceleration with a short sequence of impulses whose individual responses cancel at one target frequency — a frequency that differs per machine and per axis, and that an ADXL345 accelerometer on the toolhead measures directly. The cost is **smoothing**: the impulse sequence spreads motion over a finite window, blunting fine geometry and capping the acceleration at which the shaper remains valid.

## The mechanism being cancelled

The moving mass and the compliance of belts, gantry and frame form an under-damped second-order system. An abrupt change in commanded acceleration excites it, and the response decays over several periods. At a print speed *v* and resonant frequency *f*, successive echoes appear on the wall spaced *v / f* apart — which is why ringing is measured on a test tower rather than argued about in the slicer.

Input shaping is a feed-forward convolution applied to the toolhead trajectory. The simplest shaper, **zero vibration (ZV)**, issues two impulses separated by half the oscillation period; the vibration started by the first is met in antiphase by the second, and the residual amplitude at exactly *f* is nulled. **Modified zero vibration (MZV)** and **extra-insensitive (EI)** shapers use more impulses, widening the band over which residual vibration stays small at the price of a longer impulse window. Klipper also provides multi-hump EI variants for still wider coverage. The invariant across all of them: **cancellation is exact only at the configured frequency**, and degrades as the true resonance drifts away from it.

## Wiring the sensor

The **ADXL345** is a three-axis digital accelerometer with selectable ranges up to ±16 g and an output data rate reaching 3200 Hz, exposed over Serial Peripheral Interface (SPI) or I²C. Klipper's `[adxl345]` section configures the sensor over SPI. The sensor must be **rigidly coupled to the toolhead** — a bracket clamped to the hotend or carriage — since a compliant mount adds its own resonance to the measurement and reports it as the machine's.

```ini
[adxl345]
cs_pin: rpi:None            # SPI chip-select (example: Pi host SPI)
# spi_bus / pins depend on where the sensor is wired

[resonance_tester]
accel_chip: adxl345
probe_points:
    117, 117, 20           # X, Y, Z near the center of the bed
```

`probe_points` fixes the position the toolhead occupies while being shaken. A point near the middle of the build area at modest Z is representative; the gantry's compliance is not uniform across the volume, so the measured frequency is a property of the machine *at that point*.

## Verifying the sensor before trusting it

Two commands establish that the data are physical rather than noise.

```
ACCELEROMETER_QUERY        # reads current acceleration on all 3 axes
```

At rest one axis should read approximately `9800 mm/s^2` (one standard gravity) and the others near zero. That signature confirms the device responds and that its orientation is understood. Then:

```
MEASURE_AXES_NOISE         # baseline sensor noise per axis
```

A high or unstable noise figure indicates a loose mount, marginal wiring or interference. **A calibration run on a noisy sensor still produces a confident-looking recommendation**, because the analysis has no way to distinguish a mounting resonance or an electrical artefact from a frame mode.

## Excitation and analysis

`TEST_RESONANCES` drives the toolhead through a sweep of vibration frequencies along one axis while logging accelerometer samples, producing a frequency-response curve whose peaks are the machine's modes.

```
TEST_RESONANCES AXIS=X     # sweeps X, writes /tmp/resonances_x_*.csv
TEST_RESONANCES AXIS=Y     # sweeps Y
```

The CSV files can be plotted with Klipper's bundled `scripts/calibrate_shaper.py` to inspect the peaks directly, which shows whether the response has one dominant mode or several. The single-command path performs sweep and analysis together:

```
SHAPER_CALIBRATE           # runs both axes and picks shaper + frequency
```

`SHAPER_CALIBRATE` evaluates each available shaper type against the measured curve and reports a choice per axis:

```
Recommended shaper_type_x = mzv,   shaper_freq_x = 47.8 Hz
Recommended shaper_type_y = ei,    shaper_freq_y = 41.2 Hz
```

It also reports the **maximum acceleration** at which the selected shaper's smoothing remains acceptable. That figure, not the frequency, is the operational output: it bounds how aggressively the printer can be driven before the shaper's own smoothing becomes the limiting defect. `SAVE_CONFIG` writes the result into `printer.cfg`:

```ini
[input_shaper]
shaper_type_x: mzv
shaper_freq_x: 47.8
shaper_type_y: ei
shaper_freq_y: 41.2
```

## Why the type matters as much as the frequency

The shaper types occupy different points on one trade-off. **ZV has the shortest impulse window and therefore the least smoothing, but the narrowest band of effective cancellation.** MZV and EI tolerate a larger error between the configured and actual frequency, and pay for it with a longer window — more smoothing of fine features and a lower permissible acceleration. `SHAPER_CALIBRATE` selects by weighing residual vibration against smoothing, so soft-looking corners after calibration point toward a lower-smoothing type, and returning ghosting points toward a stale frequency.

The measured frequency is **not a permanent property of the machine**. Belt tension, toolhead mass and any change to frame stiffness move it, so a belt change or a hotend swap invalidates the calibration and calls for a repeated sweep.

A controlled comparison isolates the effect: print a ringing-tower test model at the normal print speed before calibrating and retain it, then run `SHAPER_CALIBRATE`, `SAVE_CONFIG`, and print the identical tower again. Under raking light the echoes trailing each protrusion should be absent in the second specimen.

## Pitfalls

- **A compliant sensor mount reports its own resonance.** The calibration then configures the shaper to cancel a frequency the nozzle never experiences, and ringing survives untouched.
- **Skipping `MEASURE_AXES_NOISE` yields a plausible but meaningless recommendation.** The analysis fits a shaper to whatever spectrum it receives, including electrical noise, and emits no warning that the input was garbage.
- **Applying the recommended frequency without applying the recommended maximum acceleration** leaves the printer driven past the point where the shaper's smoothing window is valid, reintroducing artefacts at speed.
- **A single dominant peak is an assumption, not a guarantee.** Where the response shows two comparable modes on one axis, a single-frequency shaper cancels one and leaves the other; the plotted CSV reveals this where the printed part does not.
- **Calibrating at one `probe_points` location and printing across the whole bed** exposes the position dependence of gantry compliance, so the residual ringing can vary across the build area.
- **Belt retensioning silently invalidates the stored `shaper_freq`.** The symptom is ghosting returning after routine maintenance, with no configuration change to blame.
