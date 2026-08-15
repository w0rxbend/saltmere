---
title: "Sensorless homing on Klipper: TMC2209 StallGuard, tuned"
date: 2026-07-31
track: cad-3dprint
summary: "A TMC2209 detects the motor stalling into the axis limit and reports it on its DIAG pin as a virtual endstop, removing the mechanical switch. This covers the wiring, the printer.cfg entries for diag_pin and driver_SGTHRS, why homing_retract_dist must be 0, the direction of the SGTHRS sensitivity scale, and the dwell that keeps homing repeatable."
reading_time: 6
tags: [klipper, tmc2209, sensorless-homing, stallguard, 3d-printing, printer-cfg]
sources:
  - title: "TMC drivers — Klipper documentation (Sensorless Homing)"
    url: "https://www.klipper3d.org/TMC_Drivers.html"
  - title: "TMC2209 Datasheet rev1.08 (Analog Devices / Trinamic, StallGuard4 & SGTHRS)"
    url: "https://www.analog.com/media/en/technical-documentation/data-sheets/TMC2209_datasheet_rev1.08.pdf"
  - title: "Configuring sensorless homing — Voron Documentation"
    url: "https://docs.vorondesign.com/tuning/sensorless.html"
  - title: "Config Reference (tmc2209) — Klipper documentation"
    url: "https://www.klipper3d.org/Config_Reference.html#tmc2209"
---

**Gist.** A mechanical limit switch adds wiring, a bracket and a connector per axis, and its repeatability is bounded by the lever geometry. The TMC2209 already derives a load measure from the motor's back-electromotive force (back-EMF), so **StallGuard4** can report the carriage driving into the frame as a digital edge on the **DIAG** pin, which Klipper consumes as a **virtual endstop**. The cost is that the trigger point is no longer a fixed mechanical feature: it is a threshold that depends on motor, run current, supply voltage and homing speed, and must be tuned per axis and re-verified from cold.

## Wiring and the DIAG pin

The driver's **DIAG** output (silkscreened DIAG on most boards) is connected to the microcontroller (MCU) pin that would otherwise carry the X or Y endstop input. Many boards provide a jumper that bridges DIAG to the endstop header directly; with that jumper set, the physical endstop connector is left empty.

The electrical behaviour of the DIAG line differs between boards, so the same threshold can produce phantom triggers on one board and none on another. Klipper's pin syntax carries the two modifiers that resolve this: **`^` enables the MCU pull-up** and **`!` inverts the logical sense** of the pin. Phantom triggers that appear before any motion are a pin-configuration symptom rather than a threshold symptom, and are addressed by changing those prefixes.

## The printer.cfg

Two sections cooperate. The `[tmc2209 stepper_x]` section declares the DIAG pin and the StallGuard threshold; the `[stepper_x]` section points its endstop at the driver's virtual endstop and disables the second homing move.

```ini
[tmc2209 stepper_x]
uart_pin: PC11
run_current: 0.700
diag_pin: ^PA1              # MCU pin wired to the driver DIAG line
driver_SGTHRS: 80          # StallGuard threshold; tune per axis (0-255)

[stepper_x]
step_pin: PB13
dir_pin: !PB12
enable_pin: !PB14
microsteps: 16
rotation_distance: 40
endstop_pin: tmc2209_stepper_x:virtual_endstop
position_endstop: 0
position_min: 0
position_max: 235
homing_speed: 40           # fast enough for reliable stall detection
homing_retract_dist: 0     # must be 0 — no second homing move
```

Three lines carry the mechanism.

`endstop_pin: tmc2209_stepper_x:virtual_endstop` binds the axis endstop to the driver's StallGuard output rather than to a board pin. The name is derived from the driver section, so a `[tmc2209 stepper_y]` section supplies `tmc2209_stepper_y:virtual_endstop`.

`homing_retract_dist: 0` **disables the second homing pass**. The default homing sequence approaches the endstop, retracts by this distance, and re-approaches slowly to refine the trigger position. That refinement is invalid here for two reasons: after the first stall the carriage is loaded against the frame, and the second approach is deliberately slow — and StallGuard's load measure is only meaningful while the motor is turning at speed. A non-zero retract distance therefore produces a second pass whose trigger point is not comparable to the first.

`driver_SGTHRS` is the trigger threshold and the only value that requires a tuning session.

## Tuning SGTHRS

StallGuard4 compares an internal load measurement against `SGTHRS`. On the TMC2209 the scale runs **0 (least sensitive) to 255 (most sensitive), so a higher `driver_SGTHRS` triggers on a smaller load**. This is inverted relative to StallGuard2 on the TMC2130, whose `driver_SGT` is more sensitive at lower values; carrying the TMC2130 intuition across is a frequent cause of tuning that moves in the wrong direction.

The two failure modes are distinguishable by symptom. **A trigger within the first few millimetres of motion means the threshold is too high**: the acceleration transient at the start of the move presents a load comparable to a stall. **A carriage that reaches the frame and grinds without the move ending means the threshold is too low**: the stall load never crosses it.

The tuning loop does not require restarting Klipper between attempts:

1. Fix `homing_speed` first and leave it fixed. Stall detection is speed-dependent, and the Klipper documentation notes the driver cannot reliably detect a stall at very slow speeds, so the homing speed has to be kept well above a crawl. Changing the speed later invalidates the threshold found at the old speed.
2. Start `driver_SGTHRS` low, run `G28 X`, and raise it.
3. Adjust the live value with `SET_TMC_FIELD STEPPER=stepper_x FIELD=SGTHRS VALUE=90` and re-home. This writes the driver register directly, so no restart is needed and the config file is untouched.
4. Search for **the highest value that never triggers on the acceleration transient** while still ending the move firmly at the limit, then back off by a few counts for margin.
5. **Leave a pause of a second or more between homing attempts.** Back-to-back `G28` invocations give inconsistent results, because the driver's stall indicator must clear and the axis must be at speed for the measurement to be meaningful.
6. Write the chosen value into `driver_SGTHRS` and confirm it still homes from a cold machine. StallGuard's readings shift as the motor warms, so the cold case is the one that must be verified rather than assumed.

## The dwell between axes

Immediately after a stall-home, the coils remain energised and hold the carriage against the frame. A homing macro that moves that axis, or homes another axis sharing the same load path, while that condition persists can corrupt the next StallGuard reading or lose steps. A short **`G4` dwell** between homing moves lets the axis settle first:

```ini
[homing_override]
gcode:
    G28 X
    G4 P250          # let X settle before Y
    G28 Y
    G4 P250
    G28 Z
```

The dwell occupies the role the mechanical second homing pass plays with a switch: instead of re-touching a contact to refine the position, it gives the driver an interval of quiet so the next axis begins its stall detection from an unloaded state.

**Extension.** Binding `SET_TMC_FIELD ... FIELD=SGTHRS` to a pair of macros that step the value up and down by five counts allows the threshold to be walked from too-low to too-high across repeated `G28 X` runs, recording the exact value at which false triggers begin, and placing `driver_SGTHRS` a few counts below it.

## Pitfalls

- **Homing ends a millimetre into the move.** The threshold is high enough that the acceleration transient reads as a stall; lower `driver_SGTHRS`, or reduce acceleration on the homing move.
- **The carriage grinds at the frame and the move never completes.** The threshold is below the stall load; raise `driver_SGTHRS`.
- **`driver_SGTHRS` is adjusted in the TMC2130 direction.** On the TMC2209, higher is more sensitive, so a value lowered to "increase sensitivity" moves the axis further into the grinding failure.
- **`homing_retract_dist` left at its default.** The second, slow approach runs against an already-loaded axis at a speed at which StallGuard is unreliable, producing a trigger position that varies run to run.
- **`homing_speed` changed after tuning.** The load measure is speed-dependent, so a threshold tuned at one homing speed does not transfer to another.
- **Threshold verified only on a warm machine.** Readings shift with motor temperature, so a value that homes reliably after a print can fail on the first home from cold.
- **Repeated `G28` with no pause.** The stall indicator has not cleared between runs, so consecutive homing attempts report different trigger points and appear to indicate an unstable threshold.
- **Phantom triggers before any motion.** This is the DIAG pin's electrical configuration, not the threshold; the `^` pull-up and `!` invert prefixes on `diag_pin` are what change it.
