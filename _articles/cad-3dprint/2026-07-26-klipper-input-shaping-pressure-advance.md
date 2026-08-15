---
title: "Klipper tuning: input shaping against ringing, pressure advance against corner blobs"
date: 2026-07-26
track: cad-3dprint
summary: "Ringing and corner blobbing are not slicer settings — they are mechanical resonance and extruder pressure lag. Klipper compensates for both in firmware: accelerometer-driven input shaping suppresses ringing, and pressure advance pre-compensates nozzle pressure at direction changes. Configuration and tuning commands included."
reading_time: 6
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

**Gist.** Two surface defects that look like slicer problems are physical: *ringing* (parallel ripples echoing a corner across the wall) is the toolhead oscillating on the compliance of belts and frame after an acceleration change, and *corner blobbing* is melt-zone pressure discharging while the toolhead slows. Klipper corrects both in firmware — **input shaping** convolves the motion command with a short impulse sequence whose induced vibrations cancel, and **pressure advance** shifts extruder position ahead of flow-rate changes. Each correction has a cost: shaping spreads a sharp move over a fixed time window and therefore rounds corners, and pressure advance mistuned in either direction converts a blob into a gap.

## Ringing as a resonance problem

Each axis is a mass — toolhead, gantry, or bed — supported on a spring formed by belts and frame compliance. A sharp velocity change excites that spring-mass system at its natural frequency. The structure oscillates while the nozzle continues to move and extrude, so **the oscillation is imprinted directly onto the wall surface**. Higher acceleration excites the spring harder, so the ripples grow more pronounced; print speed sets how far apart along the wall successive ripples land, since the nozzle covers more distance per oscillation period.

The Klipper documentation states that input shaping mitigates *remaining* resonance and is not a substitute for a rigid machine. **Stiffening the frame, correcting belt tension, and reducing moving mass change the resonance itself; shaping only cancels excitation at a frequency the machine still has.**

## Input shaping: cancellation by convolution

Input shaping is an open-loop technique. Rather than issuing the ideal step in commanded acceleration, Klipper convolves the motion command with a *shaper*: a short sequence of impulses, spaced and weighted so that **the residual vibration induced by each impulse destructively interferes with the vibration induced by the others at the target frequency**. The toolhead reaches the same endpoint; the ringing component at that frequency is suppressed.

The invariant that makes this work is also its cost. **Cancellation requires the shaper to span a fixed multiple of the resonance period, so the commanded move is smeared over that window.** The result is *smoothing*: sharp geometry is slightly rounded, and the amount of rounding scales with shaper duration. Shapers therefore trade robustness — how wide a band of frequencies stay suppressed when the real resonance drifts — against smoothing.

| Shaper | Duration | Character |
|---|---|---|
| ZV | 0.5 / freq | Shortest, least smoothing, most sensitive to frequency error |
| MZV | 0.75 / freq | Low smoothing, more tolerant of frequency error than ZV |
| ZVD | 1 / freq | More tolerant of frequency error than ZV, more smoothing |
| EI | 1 / freq | Tolerates frequency drift better than ZVD at similar duration |
| 2HUMP_EI | 1.5 / freq | Targets printers with two resonance peaks |
| 3HUMP_EI | 2 / freq | Most robust to multiple or wide resonances, most smoothing |

Durations are expressed relative to the shaper frequency, so **halving the resonance frequency doubles the smoothing window**. A low-frequency axis pays more in rounded corners for the same shaper type than a stiff, high-frequency one.

## Measuring resonances

A guessed frequency does not reliably cancel anything, because a shaper tuned away from the true peak leaves residual vibration and still pays the full smoothing cost. Klipper measures the peak with an accelerometer. The reference chip is an ADXL345 on a serial peripheral interface (SPI) bus, wired either to a spare microcontroller unit (MCU) or to a Raspberry Pi's SPI pins; Klipper also supports other accelerometers, including the MPU-9250 and LIS2DW families, each with its own bus and config section.

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

`probe_points` should sit near bed center at a safe Z height. **The accelerometer must be mounted rigidly to the toolhead** — a printed clip with screws tight, no rattle — because any compliance in the mount adds a resonance of its own to the measurement. Then, from the console:

```
ACCELEROMETER_QUERY
MEASURE_AXES_NOISE
TEST_RESONANCES AXIS=X
TEST_RESONANCES AXIS=Y
```

`ACCELEROMETER_QUERY` confirms the chip is wired and responding. `MEASURE_AXES_NOISE` reports the noise floor of the idle machine. `TEST_RESONANCES` sweeps frequencies on the named axis and writes raw acceleration samples to `/tmp`, which the bundled analysis script converts into a spectrum and a recommendation:

```
~/klipper/scripts/calibrate_shaper.py /tmp/resonances_x_*.csv -o /tmp/shaper_x.png
```

`SHAPER_CALIBRATE AXIS=X` (and `AXIS=Y`) performs the sweep, the analysis, and the recommendation in one command, and the result can be persisted with `SAVE_CONFIG`.

Without an accelerometer, a printed ringing tower — a tall thin tower carrying a bump feature, sliced at increasing speed with height — gives a coarse estimate: the frequency is back-calculated from print speed and the spacing of the ripples following the bump. **This is an approximation of the dominant peak, not a spectrum**, and it cannot reveal a second resonance peak that would call for a 2HUMP_EI shaper.

## Configuring `[input_shaper]`

```
[input_shaper]
shaper_freq_x: 52.6
shaper_freq_y: 38.2
shaper_type_x: mzv
shaper_type_y: ei
```

`shaper_type` applies to both axes when the per-axis variant is absent. `RESTART` applies the configuration. Shaping can also be changed at runtime without a restart, which allows two shapers to be compared within a single test print:

```
SET_INPUT_SHAPER SHAPER_TYPE=EI SHAPER_FREQ_X=52.6 SHAPER_FREQ_Y=38.2
```

## Pressure advance: a separate defect

Input shaping addresses vibration and has no effect on extrusion. The melt zone behaves like a compressed spring: extruding builds pressure that continues to push filament out briefly after the extruder stops, and that pressure must be rebuilt before flow resumes. At a corner the toolhead decelerates and re-accelerates, so commanded flow changes abruptly while actual flow lags. **Uncompensated, the stored pressure discharges into the slow corner as a blob; overcompensated, the retraction of the advance leaves a gap immediately after the corner.**

Pressure advance adds an extruder-position offset proportional to instantaneous flow rate, so pressure is already at the required level when the toolhead reaches the new speed.

Tuning uses a tuning tower rather than one value per print:

```
TUNING_TOWER COMMAND=SET_PRESSURE_ADVANCE PARAMETER=ADVANCE START=0 FACTOR=.005
```

The Klipper documentation gives `FACTOR=.005` for a direct-drive extruder and `FACTOR=.020` for a Bowden extruder, whose longer filament path needs larger advance values. The tower is sliced thin-walled — one perimeter, no infill, a high external perimeter speed, minimum layer time disabled — and the macro is issued immediately after the print starts; Klipper then raises `pressure_advance` as a linear function of height. Bulging corners indicate the value at that height is still too low; clean corners followed by visible gaps indicate overshoot. **With `START=0`, the tuned value is the height of the cleanest band multiplied by `FACTOR`.**

```
SET_PRESSURE_ADVANCE ADVANCE=0.045
```

```
[extruder]
pressure_advance: 0.045
pressure_advance_smooth_time: 0.040
```

`pressure_advance_smooth_time` (default 0.040 s) smooths the advance signal itself and is normally left at its default.

## Symptom-to-mechanism reference

| Symptom | Mechanism | Correction |
|---|---|---|
| Rippled echo pattern on walls, more pronounced at high acceleration | Frame or gantry resonance | Input shaping (`TEST_RESONANCES`, then `[input_shaper]`) |
| Blobs at outer corners | Stored melt pressure discharging at the direction change | Raise `pressure_advance` |
| Gaps or thin walls immediately after corners | Advance overcorrects, briefly starving flow | Lower `pressure_advance` |
| Ringing persists after shaper tuning | Mechanical looseness exceeds what shaping can cancel | Correct mechanics, re-run `SHAPER_CALIBRATE` |

The two corrections act on different physics and are tuned independently.

## Pitfalls

- **A loosely mounted accelerometer produces a spectrum of the mount.** The measured peak reflects the compliance of the clip rather than the machine, and a shaper set to it leaves the real resonance untouched while still smoothing the geometry.
- **Reusing an X-axis frequency on Y.** The two axes carry different masses and different belt paths; a shaper tuned to the wrong axis both fails to cancel and imposes its smoothing cost.
- **Selecting a long shaper on a low-frequency axis.** Shaper duration scales as a multiple of 1/freq, so 3HUMP_EI on a soft axis rounds corners visibly.
- **Tuning pressure advance while ringing is uncorrected.** Ringing near a corner is easily read as a blob or gap, leading the tower interpretation to the wrong band.
- **Changing nozzle, filament, or temperature after tuning pressure advance.** The value characterises the melt zone under those conditions; altering flow path or viscosity invalidates it.
- **Omitting `SAVE_CONFIG` after `SHAPER_CALIBRATE`.** The calibrated values apply to the running session only and are lost on restart, so the next print silently reverts to the previous configuration.
- **Interpreting the tuning tower without disabling minimum layer time.** The slicer slows layers to satisfy the minimum, changing flow rate between bands and confounding the height-to-value mapping.
