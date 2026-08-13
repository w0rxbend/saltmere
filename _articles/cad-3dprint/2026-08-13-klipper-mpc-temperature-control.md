---
title: "Klipper MPC: model-predictive hotend control that beats PID under the part fan"
date: 2026-08-13
track: cad-3dprint
summary: "MPC models your hotend as four thermal masses and feeds the heater proactively, killing the overshoot and fan-induced temperature sag that PID only reacts to. Here's the real [extruder] config and the one-command calibration — via Kalico, since mainline Klipper is still PID-only."
reading_time: 5
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

PID is reactive: it watches the error between target and measured temperature and pushes the heater harder or softer *after* the temperature has already moved. That works fine at steady state and falls apart at exactly the moments that matter — the initial heat-up (overshoot), a big flow-rate jump, and the instant the [part-cooling fan](/articles/cad-3dprint/2026-07-26-klipper-input-shaping-pressure-advance) ramps to 100% and rips 15 W of heat off the block. PID only learns about that disturbance once the thermistor has already sagged 8 °C.

**MPC (Model Predictive Control)** flips it around. Instead of tuning three gains, you give it a *physical model* of the hotend and it computes the heater power needed to hit the target, feeding disturbances forward before the temperature moves.

## Mainline vs Kalico: where MPC actually lives

Be precise about this, because it trips people up. **Mainline Klipper's `[extruder]` still supports only `control: pid` and `control: watermark`** — there is no `mpc` in the upstream config reference as of mid-2026. MPC came from Marlin, was ported to Klipper by the community, and is shipped and maintained in **[Kalico](/articles/cad-3dprint/2026-07-31-kalico-danger-klipper-fork)** (the fork formerly called Danger-Klipper). So this article is a Kalico feature. If you're on stock Klipper, you flash Kalico first.

## The model

MPC treats the hotend as **four thermal masses**: ambient air, the heater block, the temperature sensor, and the filament. Heater power warms the block; the block leaks heat to ambient (worse when the fan blows) and dumps heat into cold filament as it feeds; the block heats the sensor with some lag. Give it the constants and it predicts how much energy will leave over the next control interval and pre-compensates.

## The config

```ini
[extruder]
# ... your steps, nozzle, thermistor as usual ...
control: mpc
heater_power: 50          # nameplate heater wattage (e.g. 24V/50W)
cooling_fan: fan          # the part-cooling fan section MPC compensates for
filament_diameter: 1.75
filament_density: 1.20    # g/cm^3 (PLA ~1.2, PETG ~1.27, ABS ~1.06)
filament_heat_capacity: 1.8   # J/g/K
# --- filled in by calibration, do not hand-guess ---
#block_heat_capacity: 22.9      # J/K
#sensor_responsiveness: 0.163   # K/s per K of block-sensor delta
#ambient_transfer: 0.148        # W/K heat loss to still air
#fan_ambient_transfer: 0.062, 0.148, 0.19  # W/K vs fan speed breakpoints
```

The two things you *must* get right are `heater_power` (read it off the cartridge — a wrong value poisons the whole model) and `cooling_fan` (so MPC knows which disturbance to feed forward). The four commented block are physical constants MPC measures for you.

## Calibrate in one command

```
MPC_CALIBRATE HEATER=extruder TARGET=220 FAN_BREAKPOINTS=3
```

This runs an automated heat-up and cool-down, measures `block_heat_capacity`, `sensor_responsiveness`, and `ambient_transfer`, then cycles the part fan through `FAN_BREAKPOINTS` speeds to map `fan_ambient_transfer`. When it finishes it prints the constants to the console; save them with `SAVE_CONFIG` and they land in your `[extruder]` block. Recalibrate only if you change the heater cartridge, sensor, or hotend — not per filament.

| | PID | MPC |
|---|---|---|
| Model | none (3 gains) | 4 thermal masses |
| Fan disturbance | reacts after sag | fed forward |
| Heat-up overshoot | tuned trade-off | minimal by design |
| Tuning input | `PID_CALIBRATE` | `MPC_CALIBRATE` + heater watts |
| Noisy thermistor | can hunt | tolerant |

## Field notes

- Set `filament_density`/`heat_capacity` per material family if you print a lot of one — the filament-cooling term is small but real at high flow.
- MPC's steady-state hold is quieter than an aggressively tuned PID, so if you were living with ±1 °C ripple, expect it to tighten.
- It works on the [bed heater](/articles/cad-3dprint/2026-08-03-beacon-cartographer-eddy-probe) too, but the payoff there is smaller — beds are slow and rarely fan-disturbed.

**Try next:** flash Kalico, add the `control: mpc` block with your real `heater_power`, run `MPC_CALIBRATE HEATER=extruder FAN_BREAKPOINTS=3`, then start a print and snap the part fan to 100% mid-layer — the temperature graph should barely flinch where PID used to carve a notch.
