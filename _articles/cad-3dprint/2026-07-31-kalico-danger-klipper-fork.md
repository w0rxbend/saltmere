---
title: "Kalico: the Danger-Klipper fork and a reversible migration path"
date: 2026-07-31
track: cad-3dprint
summary: "Kalico is the community Klipper fork formerly named Danger-Klipper. What it changes relative to mainline, how the git-remote migration stays reversible, and how Model Predictive Control replaces PID on the hotend."
reading_time: 6
tags: [klipper, kalico, 3d-printing, firmware, moonraker, kiauh]
sources:
  - title: "Kalico documentation — Migrating from Klipper"
    url: "https://docs.kalico.gg/Migrating_from_Klipper.html"
  - title: "KalicoCrew/kalico (GitHub)"
    url: "https://github.com/KalicoCrew/kalico"
  - title: "Kalico documentation — Model Predictive Control"
    url: "https://docs.kalico.gg/MPC.html"
  - title: "Danger-Klipper Fork Renamed To Kalico (Hackaday)"
    url: "https://hackaday.com/2024/12/11/danger-klipper-fork-renamed-to-kalico/"
---

**Gist.** Mainline Klipper hard-codes several safety constraints and has not merged a number of proposed kinematics and heater-control features, so configurations that depend on them fail at startup. Kalico — the community fork renamed from Danger-Klipper in December 2024 — carries those features, including a `[danger_options]` section that disables specific upstream checks and a Model Predictive Control (MPC) heater algorithm. The cost is that the installation now tracks a fork's main branch rather than a tagged upstream release, and some of the unlocked options remove protections that would otherwise halt the printer.

## Identity of the fork

The project is hosted at `KalicoCrew/kalico` on GitHub. Hackaday reported the rename from Danger-Klipper to Kalico in December 2024. The firmware is the same codebase under a different label — the rename changed no functional behaviour.

Kalico tracks upstream Klipper closely and adds work that mainline has not merged. The categories are:

- **`[danger_options]`** — a configuration section whose keys relax constraints that mainline Klipper enforces unconditionally. Documented options include `temp_ignore_limits` (ignore a sensor's `min_temp`/`max_temp`), `error_on_unused_config_options` (defaults to `True`; setting it `False` stops unrecognised keys from aborting startup), `multi_mcu_trsync_timeout`, `homing_elapsed_distance_tolerance`, and `autosave_includes`, which makes `SAVE_CONFIG` recurse into `[include]` blocks instead of writing only to the top-level file.
- **Heater control beyond standard PID** — Model Predictive Control, dual-loop PID, and velocity PID.
- **Per-axis limits** — independent acceleration and velocity for X and Y on CoreXY, CoreXZ, and Cartesian kinematics, where mainline applies one pair of limits to both axes.
- **Unstable modules** — features the fork ships but does not consider settled, including non-linear extrusion, dockable-probe automation, and a G-code shell-command extension.

The load-bearing point about `[danger_options]` is that each key **removes a check rather than adding a compensating one**. With `temp_ignore_limits` set, a thermistor whose reading has gone out of range no longer triggers the shutdown that mainline would raise; the heater control loop continues to act on that reading.

## A migration that can be reversed with one checkout

Klipper is installed as a git working copy, conventionally at `~/klipper`, and the service runs from that directory. Kalico is a fork of the same repository, so its history shares ancestry with upstream and can be fetched into the existing checkout as a second remote. Switching firmware then reduces to switching branches, and switching back is symmetric.

The configuration directory `~/printer_data` is not part of the checkout and is left in place by this procedure. **Add-on modules installed into the Klipper tree — Beacon, led-effect and similar — are not carried across and must be reinstalled after the switch**, because their installers patch files inside `~/klipper`.

```bash
cd ~/klipper
git remote add kalico https://github.com/KalicoCrew/kalico.git
git checkout -b upstream-main origin/master   # bookmark mainline before moving
git fetch kalico
git checkout -b kalico-main kalico/main
sudo systemctl restart klipper
sudo systemctl restart moonraker
```

The branch `upstream-main` is the escape hatch: `git checkout upstream-main` followed by a service restart returns the installation to stock Klipper. Nothing in the procedure deletes upstream history, so the return path does not require network access.

Moonraker's update manager tracks the configured repository and channel, and will report the checkout as invalid or attempt to reset it if left pointing at mainline. The channel selects what the updater considers an update:

```ini
[update_manager klipper]
channel: dev     # 'dev' follows branch commits; 'stable' follows tagged releases
```

KIAUH v6 supports the same switch through a menu. Copy `default.kiauh.cfg` to `kiauh.cfg`, add the line `https://github.com/KalicoCrew/kalico, main`, then use the **Settings** menu to change the Klipper source repository.

## Model Predictive Control on the hotend

A PID controller acts on measured error: the drive it applies is a function of the difference between setpoint and sensor reading, so **a disturbance must first appear as a temperature deviation before the controller responds to it**. On a hotend the two large disturbances — cold filament entering the melt zone during extrusion, and forced convection when the part-cooling fan changes speed — are both known before their thermal effect reaches the sensor.

MPC instead maintains a model of the hotend and computes the drive the model predicts is required. The model's parameters are the ones declared in the configuration: heater cartridge power, the fan that cools the hotend, and the filament's diameter, density and specific heat capacity, from which the heat carried away per millimetre of extrusion follows.

```ini
[extruder]
control: mpc
heater_power: 50            # heater cartridge wattage
cooling_fan: fan            # fan that cools the hotend
filament_diameter: 1.75
filament_density: 1.20      # g/cm^3, PLA-ish
filament_heat_capacity: 1.8 # J/g/K
```

Calibration measures the block's thermal behaviour on the actual machine, including the cooling contribution of the fan at several speeds:

```
MPC_CALIBRATE HEATER=extruder TARGET=220 FAN_BREAKPOINTS=3
SAVE_CONFIG
```

`TARGET` is the temperature the calibration heats to and `FAN_BREAKPOINTS` sets how many fan speeds the sweep samples; the fitted values are written back by `SAVE_CONFIG`. Ambient temperature enters the model as a loss term. If `ambient_temp_sensor` is left unset, MPC estimates ambient itself; pointing it at a chamber sensor supplies a measured value, which matters most at cold start when the estimator has no history to work from.

## What tracking a fork costs

Kalico is a fork, not a downstream release of upstream Klipper. On `channel: dev` the installation follows the fork's main branch, so **every commit merged there is an update the machine may pull**, without the review interval that a tagged release implies. `channel: stable` follows tagged releases instead, which trades feature latency for a fixed set of commits between updates.

## Pitfalls

- **`temp_ignore_limits` suppresses the thermal shutdown, not the fault.** A thermistor with an open or shorted connection reports an out-of-range value; with limits ignored, the printer keeps heating on that reading instead of halting.
- **`error_on_unused_config_options: False` hides typos.** A misspelled key is silently ignored rather than aborting startup, so the option it was meant to set retains its default and the machine behaves as if the line were absent.
- **Switching branches alone does not switch firmware.** The running `klipper` service holds the code it started with, so the checkout and the process disagree until the service is restarted.
- **Add-on modules break silently after the switch.** Beacon, led-effect and similar install files into `~/klipper`; the branch change replaces that tree, and the printer starts with the module's config section referring to code that is no longer present.
- **Moonraker fights an unannounced repository change.** Until `[update_manager klipper]` names the fork, the updater compares the checkout against upstream and treats the divergence as a corrupt installation.
- **MPC without calibration runs on declared, not measured, parameters.** Setting `control: mpc` and restarting yields a model whose block mass and cooling curve were never fitted to the machine, so tracking during fan transitions can be worse than the PID it replaced.
- **`SAVE_CONFIG` writes only the top-level file unless `autosave_includes` is set.** With calibration data expected to land in an included file, the values are written elsewhere and the include appears not to have been updated.
