---
title: "Eddy-Current Bed Probing in Klipper: Beacon, Cartographer, and BTT Eddy"
date: 2026-08-03
track: cad-3dprint
summary: "How inductive eddy-current probes read nozzle-to-bed distance for fast full-bed scanning, and how mainline Klipper's [probe_eddy_current] is wired up with drive-current and temperature calibration."
reading_time: 7
tags: [klipper, 3d-printing, eddy-current, beacon, cartographer, btt-eddy, bed-mesh, probing]
sources:
  - title: "Eddy Current Inductive probe — Klipper documentation"
    url: "https://www.klipper3d.org/Eddy_Probe.html"
  - title: "Config Reference — Klipper documentation"
    url: "https://www.klipper3d.org/Config_Reference.html"
  - title: "BigTreeTech Eddy — GitHub"
    url: "https://github.com/bigtreetech/Eddy"
  - title: "Beacon — Surface Scanner"
    url: "https://beacon3d.com/"
  - title: "Scan vs Touch Modes — Cartographer 3D docs"
    url: "https://docs.cartographer3d.com/cartographer-probe/classic-vs-survey-touch"
---

**Gist.** Point-by-point bed probing is slow because the toolhead must stop, descend and retract at every sample. An eddy-current probe replaces the trigger event with a **continuous inductance measurement**, so distance is known at every instant and a mesh can be collected during horizontal travel. The cost is that a frequency reading has **no inherent zero and drifts with temperature**, so the probe requires drive-current calibration, a frequency-to-height model, and either a contact reference or software thermal compensation.

## The measurement

An inductive proximity sensor and an eddy-current surface scanner are the same physics component read at different resolutions. Both drive an alternating current through a coil, which establishes an oscillating magnetic field. Near a conductive bed — aluminium, spring steel, most polyetherimide (PEI) plates on steel — that field induces circulating *eddy currents* in the metal. Those currents oppose the coil's field and change the coil's effective inductance.

The two device classes differ in what they do with that change. An inductive probe compares the inductance against a threshold and toggles a digital pin: the output is one bit, asserted once per approach. An eddy-current probe measures the coil's resonant frequency continuously with an inductance-to-digital converter (LDC) such as the Texas Instruments LDC1612, and reports a number throughout the motion. **Every capability below follows from that single difference: a stream of values rather than a single edge.**

## What a continuous reading permits

Because height is known at every instant, the toolhead need not stop at each probe point. It can traverse the bed while the LDC streams frequency data, and Klipper converts that stream into a mesh. Klipper exposes three distinct probing behaviours:

- the **default descend-and-trigger mode**, used for quad gantry levelling (QGL) and Z-tilt, which reproduces the classic probe contract;
- **`scan` mode**, which samples at the current Z without descending;
- **`rapid_scan` mode**, which reads *during* horizontal motion and is the mode intended for mesh generation.

The ordering of the trade-off is documented: `rapid_scan` is faster and noisier than a settling scan. The common division of labour is therefore a settling scan for calibration and `rapid_scan` for per-print adaptive meshing.

## The missing zero

A frequency reading is a distance from the *coil* to metal. **The coil has no knowledge of where the nozzle tip is**, so the sensor cannot supply an absolute Z origin on its own; the frequency-to-height model must be anchored by an external reference.

**Contact (tap) mode** supplies that anchor. The nozzle is driven gently into the bed until motion stalls, then lifted, and the sensor examines the frequency's *rate of change* to locate the contact point. Because the quantity of interest is a slope inflection rather than an absolute frequency, the result is largely independent of the coil's absolute offset and of temperature. Beacon sells this as its "contact" hardware and Cartographer as "Survey Touch"; scanning remains the speed mechanism, contact the accuracy mechanism.

## Thermal drift

Eddy-current sensing is strongly temperature-dependent. The coil's inductance, the LDC oscillator, the printed circuit board and the expansion of nearby metal all shift with heat. The Klipper documentation states the constraint directly: calibration and the probing that uses it "should be done at the same temperature." A probe calibrated on a cold bed reads a different absolute distance once the bed is hot.

Two mitigations exist, and they are independent:

1. **Contact mode**, which measures a slope inflection rather than an absolute value and therefore largely sidesteps drift.
2. **Software compensation.** Mainline Klipper pairs a `[temperature_probe]` section with the probe, records how readings drift against an onboard thermistor, and applies the correction in real time. BTT Eddy carries an onboard temperature sensor for this purpose.

**The two sections must share a name** — `[probe_eddy_current btt_eddy]` and `[temperature_probe btt_eddy]` — for Klipper to associate them.

## Mainline Klipper and vendor forks

Three code paths coexist, and configuration snippets are not portable between them:

- **Mainline Klipper** ships `[probe_eddy_current]` and `[ldc1612]` in-tree. **BTT Eddy** is the reference hardware for this path: an LDC1612 board communicating over I2C with an onboard RP2040 running the Klipper microcontroller firmware, requiring no external plugin. The community plugin **eddy-ng** (vvuk) layers an additional tap workflow on top.
- **Beacon** ships its own `beacon_klipper` module instead of the mainline section; its Rev H hardware combines eddy scanning with a contact probe, and its commands carry a `beacon` prefix.
- **Cartographer** historically ran its own fork or module exposing "Classic" scan and "Survey Touch" modes (`CARTOGRAPHER_CALIBRATE`, `CARTOGRAPHER_TOUCH_HOME`); newer hardware also works against the mainline eddy path.

The failure mode when mixing guides is immediate and legible: **`CARTOGRAPHER_*` macros do not exist on a mainline BTT Eddy install**, and the mainline `PROBE_EDDY_CURRENT_*` commands do not exist on a Cartographer fork. For BTT Eddy the mainline path is maintained in the Klipper tree and does not depend on an out-of-tree plugin surviving the next Klipper release.

## Mainline configuration and calibration order

A minimal BTT Eddy setup on mainline Klipper:

```ini
[mcu eddy]
serial: /dev/serial/by-id/usb-Klipper_rp2040_XXXXXXXX-if00

[probe_eddy_current btt_eddy]
sensor_type: ldc1612
i2c_mcu: eddy
i2c_bus: i2c0f
z_offset: 0.5
x_offset: 0
y_offset: 20

[temperature_probe btt_eddy]
sensor_type: Generic 3950
sensor_pin: eddy:gpio26
```

Calibration is ordered, and the order is load-bearing. **Drive current is established first**, with the coil parked roughly 20 mm above bed centre; only then is frequency mapped to height:

```
LDC_CALIBRATE_DRIVE_CURRENT CHIP=btt_eddy
PROBE_EDDY_CURRENT_CALIBRATE CHIP=btt_eddy
```

`PROBE_EDDY_CURRENT_CALIBRATE` steps the nozzle downwards and requires each height to be set by the paper test through `TESTZ` and `ACCEPT`, which is what builds the distance model. Contact (tap) probing is not part of the mainline `[probe_eddy_current]` surface; it comes from vendor modules or from the eddy-ng plugin, each with its own calibration commands. Thermal-drift correction is characterised with `TEMPERATURE_PROBE_CALIBRATE PROBE=btt_eddy TARGET=<temp>` while the bed heats. `SAVE_CONFIG` persists the results.

**The drive current is specific to the coil's mounting geometry**, so `LDC_CALIBRATE_DRIVE_CURRENT` has to be re-run — and the frequency-to-height calibration redone after it — whenever the probe is remounted or its height above the bed changes.

## Comparison with other probe classes

Against a **standard inductive probe**: identical physics, but the eddy scanner yields continuous distance and full-bed scanning where the inductive probe only crosses a threshold, and the inductive trigger point is itself sensitive to temperature and to bed metal.

Against a **BLTouch**: eddy probes are non-contact for meshing, so there is no deploy servo to fail, but they require calibration and thermal management; the BLTouch carries no model and works on any bed.

Against a **load-cell or strain-gauge tool** (a Voron Tap-style mount or a load-cell toolhead): the load cell measures true nozzle contact and is indifferent to bed material and temperature, but it cannot produce a mesh without physically touching every point.

An eddy probe with contact mode occupies the middle: **non-contact scanning for the mesh, a contact reference for absolute Z**, at the cost of the most involved calibration of the group.

## Pitfalls

- **`LDC_CALIBRATE_DRIVE_CURRENT` skipped or stale after remounting.** Symptom: probing results disagree with the stored distance model after the probe is moved. Cause: the drive current is calibrated for one mounting geometry, and the frequency-to-height model is built on top of it.
- **Calibration performed cold, probing performed hot.** Symptom: first-layer Z is consistently wrong once the bed reaches temperature. Cause: coil inductance, oscillator and surrounding metal all drift with heat; the Klipper documentation requires calibration and probing at the same temperature.
- **`[temperature_probe]` named differently from `[probe_eddy_current]`.** Symptom: drift compensation never applies. Cause: Klipper associates the two sections by shared name.
- **Copying a Cartographer guide onto a mainline BTT Eddy install.** Symptom: `CARTOGRAPHER_CALIBRATE` or `CARTOGRAPHER_TOUCH_HOME` reports an unknown command. Cause: those macros belong to the Cartographer module, not to mainline `[probe_eddy_current]`.
- **Using `rapid_scan` for calibration measurements.** Symptom: mesh values that differ between runs of the same bed. Cause: `rapid_scan` samples during motion and is noisier than a settling scan.
- **Expecting the sensor to supply an absolute Z origin.** Symptom: mesh shape is correct but the whole surface sits at the wrong offset. Cause: the coil measures its own distance to metal, not the nozzle tip position; the origin comes from `z_offset` or from a contact probe.
