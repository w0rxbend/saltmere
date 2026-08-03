---
title: "Eddy-Current Bed Probing in Klipper: Beacon, Cartographer, and BTT Eddy"
date: 2026-08-03
track: cad-3dprint
summary: "How inductive eddy-current probes read nozzle-to-bed distance for fast full-bed scanning, and how to wire up mainline Klipper's [probe_eddy_current] with drive-current and temperature calibration."
reading_time: 6
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

An inductive proximity sensor and an eddy-current surface scanner are the same physics component pushed to different ends of its resolution curve. Both drive an AC current through a coil, which sets up an oscillating magnetic field. Bring that coil near a conductive bed — aluminum, spring steel, most PEI-on-steel plates — and the field induces circulating *eddy currents* in the metal. Those currents oppose the coil's field, which changes the coil's effective inductance. A cheap inductive probe just watches for the inductance to cross a threshold and trips a digital pin. An eddy-current probe instead measures the coil's resonant frequency continuously with a dedicated LDC (inductance-to-digital converter) chip like the TI LDC1612, so it reports an *analog* distance the whole time it's moving. That single difference — a number instead of a click — is what unlocks everything interesting here.

## Why a continuous reading changes the game

Because the sensor knows its height at every instant, the toolhead never has to stop, descend, and retract at each probe point. It can fly across the bed at travel speed while the LDC streams frequency data, and Klipper converts that stream into a full mesh. A 100-point mesh that takes a BLTouch several minutes drops to seconds. Klipper exposes this as distinct probing behaviors: the default descend-and-trigger mode (used for QGL and Z-tilt), a `scan` mode that samples at the current Z without moving down, and a `rapid_scan` mode that reads *during* horizontal motion for mesh generation. The tradeoff is honest — `rapid_scan` is faster but noisier than a settling scan, so people usually reserve careful scans for calibration and rapid scans for per-print adaptive meshing.

The catch with any frequency-based reading is that it has no inherent zero. The coil doesn't know where the nozzle tip is; it only knows its own distance to metal. So these probes need a physical reference. That's where **contact / tap mode** comes in: the nozzle is driven gently into the bed until motion stalls, then lifted, and the sensor watches the frequency's *rate of change* to pin the exact contact point. Tap makes the measurement independent of absolute coil offset and largely temperature-independent, which is why Beacon markets its "contact" hardware and Cartographer its "Survey Touch" mode as the accuracy story, while plain scanning is the speed story.

## The thermal-drift problem

Eddy-current sensing is exquisitely sensitive to temperature. The coil's inductance, the LDC oscillator, the PCB, and even the expansion of nearby metal all shift with heat. Klipper's docs are blunt about it: calibration and the probing that uses it "should be done at the same temperature." A probe calibrated on a cold bed will read a different absolute distance once the bed hits 100 C. Two mitigations exist. First, use tap/contact mode, which measures a slope inflection rather than an absolute value and mostly sidesteps drift. Second, use software compensation: mainline Klipper pairs a `[temperature_probe]` section with the probe, records how readings drift versus an onboard thermistor, and corrects in real time. BTT Eddy ships a temperature sensor on-board specifically for this.

## Mainline Klipper vs vendor forks

As of 2026 the landscape splits three ways, and it matters for setup:

- **Mainline Klipper** ships built-in `[probe_eddy_current]` + `[ldc1612]` support. **BTT Eddy** is the reference hardware for this path — it's an LDC1612 board that talks I2C to an onboard RP2040 running the Klipper MCU firmware, so no external plugin is required. A community plugin, **eddy-ng** (vvuk), adds a more polished tap workflow on top for those who want it.
- **Beacon** ships its own `beacon_klipper` module rather than using the mainline section; its Rev H hardware combines eddy scanning with a contact probe, and configuration uses `beacon`-prefixed commands.
- **Cartographer** historically ran its own Klipper fork/module with "Classic" scan and "Survey Touch" modes (`CARTOGRAPHER_CALIBRATE`, `CARTOGRAPHER_TOUCH_HOME`); newer hardware also works against the mainline eddy path. Reconcile before copying configs: a Cartographer guide's `CARTOGRAPHER_*` macros will not exist on a mainline BTT Eddy install, and vice versa.

If you buy BTT Eddy, prefer the mainline path — it's maintained in the Klipper tree and doesn't depend on an out-of-tree plugin surviving the next Klipper release.

## A mainline config + calibration walk-through

A minimal BTT Eddy setup on mainline Klipper looks like this. The probe and temperature sections must share a name so Klipper links them:

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
# descend_z: 0.5   # how close to approach before switching to fine scan

[temperature_probe btt_eddy]
sensor_type: Generic 3950
sensor_pin: eddy:gpio26
```

Then calibrate, in order. First find the LDC drive current with the coil parked ~20 mm above bed center, then map frequency to Z height:

```
LDC_CALIBRATE_DRIVE_CURRENT CHIP=btt_eddy
PROBE_EDDY_CURRENT_CALIBRATE CHIP=btt_eddy
```

`PROBE_EDDY_CURRENT_CALIBRATE` walks the nozzle down in steps and asks you to set each height with the paper test (via `TESTZ`/`ACCEPT`), building the distance model. If you want contact tap probing, add:

```
PROBE_EDDY_CURRENT_TAP_CALIBRATE CHIP=btt_eddy TAP=guess
```

then re-run with `TAP=refine` and `TAP=verify`. To enable thermal-drift correction, characterize the drift with `TEMPERATURE_PROBE_CALIBRATE PROBE=btt_eddy TARGET=<temp>` while the bed heats. Save everything with `SAVE_CONFIG`. Drive current is the parameter most people forget — the wrong value clips the LDC's range and produces a distance model that goes nonlinear near the bed, so always run `LDC_CALIBRATE_DRIVE_CURRENT` first after any mounting-height change.

## How it stacks up against other probes

Against a standard inductive probe: same physics, but eddy scanners give continuous distance and full-bed scanning, where an inductive probe only trips a threshold and is notoriously temperature- and metal-sensitive at the trigger point. Against BLTouch: eddy probes are non-contact for meshing (faster, no deploy servo to fail) but need real calibration and thermal management; BLTouch is dumber but forgiving and works on any bed. Against a load-cell / strain-gauge tool (like a Voron Tap or a load-cell toolhead): load cells measure true nozzle contact directly and don't care about bed material or temperature, which is the gold standard for Z accuracy — but they can't scan a mesh without touching every point. Eddy probes with tap try to get the best of both: fast non-contact meshing *plus* a contact reference for the absolute Z, at the cost of the most involved calibration of the bunch.

**Try next:** Wire up a BTT Eddy on mainline Klipper, run `LDC_CALIBRATE_DRIVE_CURRENT` then `PROBE_EDDY_CURRENT_CALIBRATE`, and compare a `rapid_scan` adaptive mesh time against your current BLTouch or inductive probe.
