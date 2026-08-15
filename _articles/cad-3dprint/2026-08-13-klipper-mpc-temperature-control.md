---
title: "Klipper MPC: model-predictive hotend control under part-fan disturbance"
date: 2026-08-13
track: cad-3dprint
summary: "Model predictive control represents the hotend as four thermal masses and feeds heater power forward, compensating fan-induced heat loss before the sensor registers it. Covers the [extruder] configuration, the MPC_CALIBRATE procedure, and why the feature lives in Kalico rather than mainline Klipper."
reading_time: 6
tags: [klipper, mpc, hotend, temperature, kalico]
sources:
  - title: "Model Predictive Control — Kalico documentation"
    url: "https://docs.kalico.gg/MPC.html"
  - title: "Model Predictive Temperature Control port for Klipper (Klipper Discourse)"
    url: "https://klipper.discourse.group/t/model-predictive-temperature-control-port-for-klipper/12850"
  - title: "Model Predictive Temperature Control — Marlin Firmware"
    url: "https://marlinfw.org/docs/features/model_predictive_control.html"
  - title: "KalicoCrew/kalico (GitHub)"
    url: "https://github.com/KalicoCrew/kalico"
---

**Gist.** A proportional-integral-derivative (PID) controller acts only on the measured error, so it cannot respond to a heat-loss disturbance until the thermistor has already cooled. Model predictive control (MPC) replaces the three gains with a physical model of the hotend — heater block, sensor, ambient air, filament — and computes the heater power required to hold the target, adding a feed-forward term for known disturbances such as the part-cooling fan. The cost is that the model must be parameterised: the heater wattage must be stated correctly by hand, and four thermal constants must be measured by a calibration run that is invalidated whenever the heater cartridge, sensor or hotend changes.

## The reactive limit of PID

A PID loop computes heater output from the error `target − measured` and its integral and derivative. Every term is a function of an error that has already occurred. At steady state this is adequate. It degrades at three moments: initial heat-up, where the integral term accumulates during the climb and produces overshoot; a large flow-rate step, where cold filament absorbs energy the loop has not budgeted for; and the instant the [part-cooling fan](/articles/cad-3dprint/2026-07-26-klipper-input-shaping-pressure-advance) ramps, which raises convective loss from the block. In the fan case the controller receives no signal at all until the block, and then the sensor, have cooled — and the sensor lags the block. **The disturbance is fully known at the moment the fan command is issued, and PID structurally cannot use that information.**

## Mainline Klipper versus Kalico

**Mainline Klipper's `[extruder]` section offers no `mpc` control type**; the upstream configuration reference documents no such value as of mid-2026. The technique originates in Marlin firmware, was ported to Klipper by the community through the Klipper Discourse thread cited below, and is shipped and maintained in **[Kalico](/articles/cad-3dprint/2026-07-31-kalico-danger-klipper-fork)**, the fork formerly named Danger-Klipper. Everything below is therefore a Kalico feature and requires flashing Kalico first.

## The four-mass model

MPC represents the hotend as **four thermal masses: ambient air, the heater block, the temperature sensor, and the filament**. The energy flows between them are:

- Heater power, in watts, raises the block temperature at a rate set by `block_heat_capacity` (joules per kelvin). A given wattage produces a predictable slope only if the stated wattage is correct.
- The block loses heat to ambient at `ambient_transfer` watts per kelvin of block-to-ambient difference. This coefficient rises with airflow, and it is measured separately at several fan speeds as `fan_ambient_transfer`.
- The block heats the sensor at a rate governed by `sensor_responsiveness`, expressed as kelvin per second per kelvin of block-to-sensor difference. **This term is what lets the controller reason about the block temperature rather than the delayed sensor reading** — the sensor is modelled as a lagging observer of the block, not as the block itself.
- Filament entering the melt zone carries away energy proportional to volumetric flow, filament density and specific heat capacity. That is the role of `filament_diameter`, `filament_density` and `filament_heat_capacity`.

With those constants the controller predicts the energy that will leave the block over the coming control interval and sets heater power to replace it, rather than waiting for the loss to appear as an error.

## Configuration

```ini
[extruder]
# ... steps, nozzle, thermistor as usual ...
control: mpc
heater_power: 50          # nameplate heater wattage (e.g. 24 V / 50 W)
cooling_fan: fan          # part-cooling fan section MPC compensates for
filament_diameter: 1.75
filament_density: 1.20    # g/cm^3 (PLA ~1.2, PETG ~1.27, ABS ~1.06)
filament_heat_capacity: 1.8   # J/g/K
# --- filled in by calibration, not by hand ---
#block_heat_capacity: 22.9      # J/K
#sensor_responsiveness: 0.163   # K/s per K of block-sensor delta
#ambient_transfer: 0.148        # W/K heat loss to still air
#fan_ambient_transfer: 0.062, 0.148, 0.19  # W/K at fan-speed breakpoints
```

Two entries are not measurable by the calibration routine and must be stated correctly. **`heater_power` is the nameplate wattage of the cartridge; because the model converts commanded power into a predicted temperature slope, an incorrect value biases every subsequent prediction** and the measured constants absorb the error into themselves. **`cooling_fan` names the fan section whose speed is fed forward**; without it the fan remains an unmodelled disturbance and the controller reverts to correcting after the fact.

## Calibration

```
MPC_CALIBRATE HEATER=extruder TARGET=220 FAN_BREAKPOINTS=3
```

The routine performs an automated heat-up and cool-down to measure `block_heat_capacity`, `sensor_responsiveness` and `ambient_transfer`, then cycles the part-cooling fan through `FAN_BREAKPOINTS` speeds to map `fan_ambient_transfer` across that range. The resulting constants are printed to the console; `SAVE_CONFIG` writes them into the `[extruder]` section. **The measured constants describe the hardware, not the material, so recalibration is required when the heater cartridge, sensor or hotend changes — not per filament.** Filament properties are handled by the three filament fields, which may be adjusted per material family.

| | PID | MPC |
|---|---|---|
| Model | none (three gains) | four thermal masses |
| Fan disturbance | corrected after the sensor cools | fed forward from fan speed |
| Heat-up overshoot | traded against response speed by tuning | limited by the model |
| Tuning input | `PID_CALIBRATE` | `MPC_CALIBRATE` plus stated heater wattage |
| Noisy sensor | derivative term can drive hunting | sensor modelled as lagging observer |

## Scope of the benefit

The filament-cooling term is small in absolute magnitude and matters mainly at high volumetric flow, where the mass rate of cold plastic entering the block is largest. Steady-state hold is generally quieter than an aggressively tuned PID loop, because the output is derived from a model rather than from amplified error. MPC is also selectable for the [bed heater](/articles/cad-3dprint/2026-08-03-beacon-cartographer-eddy-probe), but the disturbance MPC is best at rejecting — a fast, commanded change in convective loss — rarely occurs there, and the bed's large thermal mass makes it slow to disturb in the first place.

A direct check of the mechanism: with `control: mpc` configured and calibrated, start a print and command the part fan to full speed mid-layer. The feed-forward term raises heater power at the moment of the fan command, so the temperature trace should show a far smaller notch than the same transition under PID.

## Pitfalls

- Configuring `control: mpc` on mainline Klipper fails at startup with an unknown control type; MPC exists only in Kalico, so the firmware must be flashed before the section parses.
- A `heater_power` value copied from a different cartridge silently distorts the model: the calibration run absorbs the discrepancy into `block_heat_capacity` and related constants, producing a self-consistent but wrong model that misbehaves away from the calibration point.
- Omitting `cooling_fan` leaves fan-driven heat loss outside the model, which removes the specific advantage MPC has over PID and leaves a temperature sag on fan ramps.
- Hand-entering `block_heat_capacity`, `sensor_responsiveness`, `ambient_transfer` or `fan_ambient_transfer` from another machine's configuration substitutes another hotend's physics; these are measurements of the installed hardware.
- Running `MPC_CALIBRATE` and omitting `SAVE_CONFIG` discards the constants at the next restart, since the routine only prints them to the console.
- Swapping the heater cartridge, thermistor or hotend without recalibrating leaves constants that describe hardware no longer installed.
- Setting `FAN_BREAKPOINTS` low maps `fan_ambient_transfer` at fewer speeds, so intermediate fan settings are interpolated from a coarser curve.
