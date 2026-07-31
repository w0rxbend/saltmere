---
title: "Sensorless homing on Klipper: TMC2209 StallGuard, tuned"
date: 2026-07-31
track: cad-3dprint
summary: "Skip the endstop switches: a TMC2209 can detect the motor stalling into the axis limit and report it on its DIAG pin as a virtual endstop. This covers the wiring, the printer.cfg for diag_pin and driver_SGTHRS, why homing_retract_dist must be 0, the SGTHRS tuning direction everyone gets backwards, and the dwell that keeps homing repeatable."
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

A limit switch is a small failure surface with a real cost: two more wires per axis, a bracket to print, a screw to strip, and a connector that eventually goes intermittent. On a CoreXY or a delta the switch placement is fiddly and the repeatability is only as good as the lever. The TMC2209 offers a different deal — it already measures the back-EMF load on the motor, so when the carriage drives into the frame and the motor stalls, the driver *knows*. **StallGuard4** turns that stall into a digital pulse on the **DIAG** pin, and Klipper treats that pin as a **virtual endstop**. No switch, no bracket, and the "endstop" is the physical axis limit itself.

The catch is that sensorless homing is a tuning exercise, not a plug-in feature. The stall threshold depends on your motor, current, voltage, and homing speed, and a bad value either crashes silently past the limit or triggers on the acceleration transient a millimetre after it starts moving. Get the workflow right and it's rock solid.

## Wiring and the DIAG pin

Wire the driver's **DIAG** (sometimes silkscreened DIAG/INDEX) output to the MCU pin that would otherwise be your X (or Y) endstop input. On many boards there's a dedicated jumper that bridges the driver's DIAG to the endstop header — set it, and leave the physical endstop connector empty. One important electrical note: the DIAG line is push-pull/open-ended depending on the board, so if you see phantom triggers, drop the pull-up and invert, or vice versa — the pin definition in Klipper carries the `^` (pull-up) and `!` (invert) prefixes for exactly this.

## The printer.cfg

Two sections cooperate. The `[tmc2209 stepper_x]` section declares the DIAG pin and the StallGuard threshold; the `[stepper_x]` section points its endstop at the driver's virtual endstop and disables the second homing move.

```ini
[tmc2209 stepper_x]
uart_pin: PC11
run_current: 0.700
diag_pin: ^PA1              # MCU pin wired to the driver DIAG line
driver_SGTHRS: 80          # StallGuard threshold; TUNE THIS (0-255)

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
homing_retract_dist: 0     # MUST be 0 — no second homing move
```

Three lines carry the whole trick. `endstop_pin: tmc2209_stepper_x:virtual_endstop` tells Klipper the endstop is the driver's StallGuard output, not a physical pin. `homing_retract_dist: 0` **disables the second homing pass** — the usual back-off-and-re-home dance doesn't work sensorlessly, because after the first stall the motor is loaded against the frame and a short re-approach can't build a clean stall again. And `driver_SGTHRS` is the number you'll spend your tuning session on.

## Tuning SGTHRS (the direction everyone reverses)

On the TMC2209, StallGuard4 compares an internal load measurement against `SGTHRS`, and — counterintuitively — **a higher `driver_SGTHRS` is *more* sensitive** (0 = least sensitive, 255 = most). This is the opposite of the older TMC2130 StallGuard2 `driver_SGT`, where lower is more sensitive, and mixing them up is the single most common tuning mistake. If homing triggers instantly the moment the motor moves, your threshold is too high; if the carriage slams the frame and grinds without stopping, it's too low.

The workflow, without reflashing between attempts:

1. Set `homing_speed` first and leave it (stall detection is speed-dependent — the Klipper docs note the driver can't reliably detect a stall at very slow speeds; aim for roughly a full motor revolution every two seconds or faster).
2. Start `driver_SGTHRS` low, then home `G28 X` and step it up.
3. Adjust live with `SET_TMC_FIELD STEPPER=stepper_x FIELD=SGTHRS VALUE=90` and re-home, no restart needed. Binary-search toward the highest value that never false-triggers on the acceleration transient but still stops firmly at the limit.
4. **Wait a couple of seconds between homing attempts.** StallGuard needs the motor to be moving at speed to be meaningful, and the driver's internal indicator must clear between runs — hammering `G28` back-to-back gives inconsistent results.
5. Once you've found the sweet spot, write it into `driver_SGTHRS` and confirm it survives a cold start (StallGuard behaves differently as the motor warms; verify from cold, which is the worst case).

## The dwell that makes it repeatable

There's a subtle timing issue on multi-axis or homing-override setups: right after a stall-home, the motor coils are still energized and holding position against the frame. If your homing macro immediately moves that axis (or homes the next one on a shared load), the residual load can corrupt the next StallGuard read or cause a missed step. The fix is a short **`G4` dwell** in your homing override to let the axis settle and de-energize before the next move — a couple hundred milliseconds is usually enough:

```ini
[homing_override]
gcode:
    G28 X
    G4 P250          # let X settle / de-energize before Y
    G28 Y
    G4 P250
    G28 Z
```

This is the sensorless equivalent of the mechanical second-home: instead of re-touching a switch, you give the driver a moment of quiet so the next axis's stall detection starts from a clean state.

**Try next:** Bind `SET_TMC_FIELD ... FIELD=SGTHRS` to two macros (`SG_UP`/`SG_DOWN` that bump the value by 5) and home X repeatedly while walking the threshold from too-low to too-high — note the exact value where false triggers start, then set `driver_SGTHRS` a few counts below it for margin.
