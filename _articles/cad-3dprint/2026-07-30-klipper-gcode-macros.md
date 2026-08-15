---
title: "Scripting Klipper: Custom G-code Macros"
date: 2026-07-30
track: cad-3dprint
summary: "How the [gcode_macro] section, Jinja2 templating, parameters, and printer-state access let you build a real PRINT_START and override built-ins like M600 in Klipper."
reading_time: 5
tags: [klipper, 3d-printing, gcode, macros, jinja2, firmware]
sources:
  - title: "Klipper — Command Templates"
    url: "https://www.klipper3d.org/Command_Templates.html"
  - title: "Klipper — Configuration Reference (gcode_macro, delayed_gcode)"
    url: "https://www.klipper3d.org/Config_Reference.html"
  - title: "Klipper — G-Codes"
    url: "https://www.klipper3d.org/G-Codes.html"
  - title: "Klipper — sample-macros.cfg"
    url: "https://github.com/Klipper3d/klipper/blob/master/config/sample-macros.cfg"
  - title: "Ellis' Print Tuning Guide"
    url: "https://ellis3dp.com/Print-Tuning-Guide/"
---

Marlin bakes its behavior into firmware you compile. Klipper does the opposite: your macros live in `printer.cfg` as plain text, and they're real programs — conditionals, loops, live access to printer state. A `PRINT_START` that reads its own bed temperature isn't a hack; it's how the system is meant to be used.

## The anatomy of a macro

A macro is a config section plus a `gcode:` block. Indentation matters — the body must be indented under `gcode:`.

{% raw %}
```
[gcode_macro HELLO]
description: Say hi in the console
gcode:
  M117 Hello from Klipper
```
{% endraw %}

Now `HELLO` is a first-class G-code command. Type it in the console, call it from another macro, or put it in your slicer.

## Jinja2, the way Klipper spells it

Klipper evaluates each `gcode:` block as a [Jinja2](https://www.klipper3d.org/Command_Templates.html) template *before* running it. Two constructs matter:

- **`{ ... }`** — evaluate an expression and substitute the result. (Note: single braces, not Jinja's usual {% raw %}`{{ }}`{% endraw %}.)
- **{% raw %}`{% ... %}`{% endraw %}** — a statement: `set`, `if`, `for`.

{% raw %}
```
[gcode_macro SLOW_FAN]
gcode:
  M106 S{ printer.fan.speed * 0.9 * 255 }
```
{% endraw %}

## Parameters and defaults

Arguments arrive through the `params` pseudo-variable. **Names are always upper-case, and every value is a string** — so cast it. `default()` supplies a fallback when the caller omits the argument.

{% raw %}
```
[gcode_macro SET_BED]
gcode:
  {% set t = params.TEMP|default(60)|float %}
  M140 S{ t }
```
{% endraw %}

Call it as `SET_BED TEMP=65`, or bare `SET_BED` to get 60.

## Reading printer state

The `printer` object exposes live status. A few useful paths:

| Expression | Value |
|---|---|
| `printer.toolhead.position.x` | current X (mm) |
| `printer.heater_bed.temperature` | measured bed temp |
| `printer.heater_bed.target` | requested bed temp |
| `printer.extruder.temperature` | measured nozzle temp |
| `printer["gcode_macro NAME"].var` | a variable on another macro |

Sections whose names contain spaces need bracket syntax, hence `printer["gcode_macro NAME"]`.

## A parameterized PRINT_START

This is the payoff. One macro your slicer calls with `PRINT_START BED=60 EXTRUDER=215` — it heats the bed, homes while the bed soaks, then brings the nozzle up and waits.

{% raw %}
```
[gcode_macro PRINT_START]
gcode:
  {% set bed = params.BED|default(60)|float %}
  {% set nozzle = params.EXTRUDER|default(210)|float %}

  M140 S{ bed }                    ; start bed heating (no wait)
  G28                              ; home all axes
  M190 S{ bed }                    ; wait for bed
  M104 S{ nozzle }                 ; start nozzle heating
  G1 Z5 F3000                      ; lift
  M109 S{ nozzle }                 ; wait for nozzle
  G92 E0                           ; reset extruder
```
{% endraw %}

`M140`/`M104` set-and-continue; `M190`/`M109` set-and-wait. Overlapping the homing with the bed soak shaves real time off every print.

## Variables that persist across a print

A macro can carry `variable_` state, readable and writable at runtime with `SET_GCODE_VARIABLE`. Handy for stashing a value in one macro and reading it in another.

{% raw %}
```
[gcode_macro START_PROBE]
variable_bed_temp: 0
gcode:
  SET_GCODE_VARIABLE MACRO=START_PROBE VARIABLE=bed_temp VALUE={ printer.heater_bed.target }

[gcode_macro FINISH_PROBE]
gcode:
  M140 S{ printer["gcode_macro START_PROBE"].bed_temp }
```
{% endraw %}

Variable names may not use upper-case characters, and the value is parsed as a Python literal.

## Overriding a built-in: M600

Any macro can replace an existing command with `rename_existing`, which keeps the old definition available under a new name. But the classic filament-change case, `M600`, doesn't even need that — Klipper has no built-in `M600`, so you just define it. It leans on the `[pause_resume]` module:

{% raw %}
```
[pause_resume]

[gcode_macro M600]
gcode:
  {% set X = params.X|default(50)|float %}
  {% set Y = params.Y|default(0)|float %}
  {% set Z = params.Z|default(10)|float %}
  SAVE_GCODE_STATE NAME=M600_state
  PAUSE
  G91
  G1 E-.8 F2700
  G1 Z{Z}
  G90
  G1 X{X} Y{Y} F3000
  G91
  G1 E-50 F1000
  RESTORE_GCODE_STATE NAME=M600_state
```
{% endraw %}

`SAVE_GCODE_STATE`/`RESTORE_GCODE_STATE` snapshot positioning mode and coordinates so `RESUME` picks up cleanly. To genuinely wrap a real built-in — say adding a park move to `PAUSE` — you'd write `[gcode_macro PAUSE]` with `rename_existing: BASE_PAUSE`, then call `BASE_PAUSE` inside it. The docs warn to be careful: overriding commands can cause complex and unexpected results.

## delayed_gcode, briefly

For time-based actions, `[delayed_gcode]` runs after a delay. Set `initial_duration:` to fire once at startup (init routines), or trigger it from a macro with `UPDATE_DELAYED_GCODE ID=name DURATION=10`. Re-scheduling itself from within its own block gives you a simple polling loop — useful for idle-timeout LEDs or temperature watchdogs.

**Try next:** convert your slicer's static start G-code into a `PRINT_START BED=... EXTRUDER=...` call and delete the duplicated heat/home lines from the slicer profile.
