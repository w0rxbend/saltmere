---
title: "Kalico: the Danger-Klipper fork, and how to switch without bricking your printer"
date: 2026-07-31
track: cad-3dprint
summary: "Kalico is the community Klipper fork formerly known as Danger-Klipper. Here's what it changes versus mainline, how to switch (and switch back), and one feature worth enabling: MPC hotend control."
reading_time: 5
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

If you run Klipper and keep hitting "that config option isn't supported upstream," you've probably bumped into **Kalico**. It's a community-maintained fork of Klipper — the same one that used to be called **Danger-Klipper**. The rename landed in December 2024; the old motto ("I should be able to light my printer on fire") read as reckless, so the project picked a name after the pirate Calico Jack, keeping the nautical theme Klipper and Marlin started. Same firmware, less alarming label. The GitHub home is now `KalicoCrew/kalico`, tagline "Klipper, but Limitless."

## What actually changes vs mainline

Kalico tracks upstream Klipper closely but adds features that mainline rejects or hasn't merged. The headline ones:

- **`[danger_options]`** — a config section that unlocks constraints Klipper hard-codes. Real options include `temp_ignore_limits` (ignore `min_temp`/`max_temp` on sensors), `error_on_unused_config_options` (default `True` — set `False` to stop unknown keys from aborting startup), `multi_mcu_trsync_timeout`, `homing_elapsed_distance_tolerance`, and `autosave_includes` so `SAVE_CONFIG` recurses into `[include]` blocks.
- **Advanced heater control** — Model Predictive Control (MPC), dual-loop PID, and velocity PID, beyond Klipper's standard PID.
- **Per-axis limits** — independent acceleration and velocity for X and Y on CoreXY, CoreXZ, and Cartesian.
- **More kinematics** — `hybrid-corexy`, `hybrid-corexz`, `deltesian`, rotary delta, polar, cable winch.
- **Bleeding-edge modules** — nonlinear pressure advance, dockable probe automation, automatic Z-offset calibration, and a G-code shell-command extension.

The "danger" framing is the point: these features assume you know what your machine can survive. Nothing here holds your hand.

## Switching over (the reversible way)

The cleanest migration keeps your existing `~/klipper` git checkout and adds Kalico as a remote, so you can bounce back to upstream with one `git checkout`. Your `~/printer_data` (configs, macros) is untouched, but **back it up anyway**, and note that add-on modules like Beacon or led-effect must be reinstalled after switching.

```bash
cd ~/klipper
git remote add kalico https://github.com/KalicoCrew/kalico.git
git checkout -b upstream-main origin/master   # bookmark mainline
git branch -D master
git fetch kalico main
git checkout -b main kalico/main
sudo systemctl restart klipper
sudo systemctl restart moonraker
```

To go back to stock Klipper later: `git checkout upstream-main` and restart. Then point Moonraker's updater at the fork so it stops fighting you:

```ini
[update_manager klipper]
channel: dev     # 'dev' = main branch commits; 'stable' = monthly vYYYY.MM.NN tags
```

Prefer a menu? KIAUH v6 supports custom repos: copy `default.kiauh.cfg` to `kiauh.cfg`, add the line `https://github.com/KalicoCrew/kalico, main`, then use **[S] Settings → 1** to switch the Klipper source repo.

## One feature to try: MPC hotend control

Standard PID reacts to error after it happens. **MPC** builds a thermal model of your hotend (heater power, block mass, ambient loss, and filament heat capacity) and predicts the drive it needs — so it holds temperature far better during fast retractions and when the part-cooling fan kicks on. Enable it on the extruder:

```ini
[extruder]
control: mpc
heater_power: 50            # heater cartridge wattage
cooling_fan: fan            # fan that cools the hotend
filament_diameter: 1.75
filament_density: 1.20      # g/cm^3, PLA-ish
filament_heat_capacity: 1.8 # J/g/K
```

Then calibrate the model, sweeping fan speeds so it learns the cooling curve, and persist it:

```
MPC_CALIBRATE HEATER=extruder FAN_BREAKPOINTS=7
SAVE_CONFIG
```

Leave `ambient_temp_sensor` unset to let MPC estimate ambient, or point it at a chamber sensor for a better cold-start estimate.

## Cautions

This is a fork, not a downstream release. You inherit whatever Kalico's main branch is doing, and some `[danger_options]` genuinely remove protections — `temp_ignore_limits` will happily let a thermistor read garbage without shutting down. Pin `channel: stable` if you want monthly tagged releases rather than every commit, keep the `upstream-main` branch around as your escape hatch, and don't enable danger flags you can't explain out loud.

**Try next:** On a spare printer, add Kalico as a git remote using the reversible steps above, enable `control: mpc` on the extruder, run `MPC_CALIBRATE`, and compare temperature stability against your old PID during a fast retraction-heavy print.
