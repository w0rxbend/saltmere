---
title: "Scripting Klipper: Custom G-code Macros"
date: 2026-07-30
track: cad-3dprint
summary: "How the [gcode_macro] section, Jinja2 templating, parameters, and printer-state access combine to build a real PRINT_START and override built-ins such as M600 in Klipper."
reading_time: 6
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

**Gist.** Marlin-style firmware fixes start-up and filament-change behaviour at compile time, so any change to a print sequence requires a toolchain, a flash cycle, and a reboot of the motion controller. Klipper moves the whole command layer onto the host: each `gcode:` block in `printer.cfg` is a [Jinja2](https://www.klipper3d.org/Command_Templates.html) template rendered on the host CPU immediately before dispatch, which makes conditionals, parameters, and live printer state available to ordinary configuration text. The cost is that **the template is expanded once, in full, before the first resulting command executes** — the macro sees only the state that existed at expansion time, and cannot observe the effect of its own moves.

## Anatomy of a macro

A macro is a configuration section plus an indented `gcode:` block. Indentation is significant; the body must be indented under `gcode:`.

{% raw %}
```
[gcode_macro HELLO]
description: Say hi in the console
gcode:
  M117 Hello from Klipper
```
{% endraw %}

`HELLO` then becomes a first-class G-code command, indistinguishable at the dispatch layer from `G28`: it can be typed in the console, called from another macro, or emitted by a slicer. Macro names are matched case-insensitively and are conventionally written upper-case.

## The two template constructs

Klipper renders each `gcode:` block as a Jinja2 template *before* running any of it. Two constructs carry the work:

- **`{ ... }`** — evaluate an expression and substitute its string form. Klipper uses **single braces, not Jinja2's usual double braces**; the documentation gives no rationale for the choice, and the single form is the only one the expression delimiter accepts. Writing {%- raw -%}`{{ x }}`{%- endraw -%} does not produce the expected substitution: the outer brace opens the expression and the remainder is parsed as the expression text, which normally fails as a template syntax error.
- **{% raw %}`{% ... %}`{% endraw %}** — a statement: `set`, `if`, `for`.

{% raw %}
```
[gcode_macro SLOW_FAN]
gcode:
  M106 S{ printer.fan.speed * 0.9 * 255 }
```
{% endraw %}

Rendering is a pure function of the `printer` object at expansion time. A loop of *n* iterations therefore produces *n* fully materialised command lines in a single string before any of them reaches the motion planner; a template that expands to tens of thousands of lines consumes proportional host memory and blocks the render for the duration. **Template expansion is O(output length), and the output is buffered in its entirety.**

## Parameters and defaults

Arguments arrive through the `params` pseudo-variable, a dictionary. **Parameter names are always upper-case and every value is a string**, so numeric use requires an explicit cast. `default()` supplies a fallback when the caller omits the argument.

{% raw %}
```
[gcode_macro SET_BED]
gcode:
  {% set t = params.TEMP|default(60)|float %}
  M140 S{ t }
```
{% endraw %}

`SET_BED TEMP=65` sets 65 °C; a bare `SET_BED` sets 60 °C. Omitting `|float` yields string concatenation semantics in any subsequent arithmetic — `params.TEMP * 2` on the string `"60"` produces `"6060"`, which is accepted by the template engine and rejected only later, by the heater, as an out-of-range temperature.

## Reading printer state

The `printer` object exposes the same status dictionary that the Moonraker API publishes. Frequently used paths:

| Expression | Value |
|---|---|
| `printer.toolhead.position.x` | commanded X of the last queued move (mm) |
| `printer.heater_bed.temperature` | measured bed temperature (°C) |
| `printer.heater_bed.target` | requested bed temperature (°C) |
| `printer.extruder.temperature` | measured nozzle temperature (°C) |
| `printer["gcode_macro NAME"].var` | a variable published by another macro |

Section names containing a space require bracket syntax, hence `printer["gcode_macro NAME"]`. The critical semantic detail is that **`printer.toolhead.position` reports the position after the last move already placed in the look-ahead queue, not the physical position of the carriage**, which may lag by the full queue depth. Code that needs the two to coincide must precede the read with `M400`, which drains the queue — but `M400` executes at run time, after the template has already been rendered, so a value read inside the same macro body is unaffected by it.

## A parameterised PRINT_START

The concrete payoff is a single slicer call, `PRINT_START BED=60 EXTRUDER=215`, replacing a fixed block of start G-code duplicated across every filament profile.

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

`M140` and `M104` are set-and-continue; `M190` and `M109` are set-and-wait. The ordering exploits that distinction: homing (typically 15–30 s on a bed-slinger) overlaps the bed soak rather than following it, so the wall-clock start cost falls to `max(t_bed, t_home) + t_nozzle` instead of `t_bed + t_home + t_nozzle`. Deferring nozzle heat until after homing also avoids holding molten filament at temperature over the probe sequence, which is the usual source of the ooze blob dragged into the first layer.

## Variables that persist across a print

A macro may declare `variable_`-prefixed state, published on the `printer` object and mutable at run time through `SET_GCODE_VARIABLE`. This is the only supported channel for passing a value from one macro invocation to another.

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

Variable names may not contain upper-case characters, and the assigned value is parsed as a Python literal — a bare word is a syntax error, whereas `VALUE="'left'"` supplies the string `left`. Because `SET_GCODE_VARIABLE` executes at run time and the reader's template renders at *its* expansion time, **a variable written and read inside the same macro body still yields the pre-write value**; the write is only visible to a macro expanded afterwards.

## Overriding a built-in: M600

`rename_existing` replaces an existing command while retaining the original under a new name. The canonical filament-change case does not need it: Klipper defines no built-in `M600`, so the section creates one, delegating the state machine to the `[pause_resume]` module.

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

`SAVE_GCODE_STATE` and `RESTORE_GCODE_STATE` snapshot and restore the interpreter state — absolute versus relative positioning (`G90`/`G91`), extruder mode (`M82`/`M83`), origin offsets, and speed factor — so `RESUME` resumes against the same coordinate frame the sliced file assumed. Without the pair, the trailing `G91` above would leak, and the first move after resume would be interpreted as a relative displacement of the sliced absolute coordinate.

Wrapping a genuine built-in follows the same shape: `[gcode_macro PAUSE]` with `rename_existing: BASE_PAUSE`, calling `BASE_PAUSE` from inside the new body. The **invariant is that the renamed original must be invoked exactly once on every path through the override**; skipping it on a conditional branch leaves `[pause_resume]` believing the printer is running while the toolhead sits parked. The Klipper documentation warns explicitly that overriding commands can produce complex and unexpected results.

## delayed_gcode

`[delayed_gcode]` schedules a block to run after a delay measured in seconds. `initial_duration:` fires it once after configuration load, which is the standard hook for initialisation that must not run during the config parse. `UPDATE_DELAYED_GCODE ID=name DURATION=10` (re)arms it from a macro, and `DURATION=0` cancels a pending timer. A block that re-arms itself constitutes a polling loop suitable for idle-timeout lighting or a temperature watchdog; the effective period is the requested duration plus the render and execution time of the block, so it drifts rather than holding a fixed phase.

## Pitfalls

- **A `SET_GCODE_VARIABLE` write is invisible to the rest of the same macro.** The whole body is rendered before the first command executes, so subsequent `{ ... }` reads return the pre-write value.
- **A parameter used without `|float` or `|int` behaves as a string.** `params.TEMP * 2` on `"60"` renders `"6060"`, and the failure surfaces later as an out-of-range heater command rather than a template error.
- **`printer.toolhead.position` reflects the look-ahead queue, not the carriage.** Reading it to compute a return move after an interrupted print yields the last queued destination; only an `M400` before the *next* macro's expansion synchronises the two.
- **A parking move without `SAVE_GCODE_STATE` leaks positioning mode.** A `G91` left set at the end of a filament-change macro turns the first absolute move after `RESUME` into a relative one, driving the toolhead off the bed or into the part.
- **`rename_existing` skipped on a conditional branch desynchronises `[pause_resume]`.** The module's paused flag never sets, `RESUME` reports no paused print, and the queued file continues from the parked position.
- **Upper-case characters in a `variable_` name are rejected at config load,** so the printer refuses to start rather than failing at the point of use.
- **An unbounded `{% raw %}{% for %}{% endraw %}` loop materialises every generated line in host memory before execution begins,** producing a multi-second stall and, at large counts, an out-of-memory abort on a single-board host.
- **A self-rearming `[delayed_gcode]` drifts.** Its period is the requested duration plus render and execution time, so it must not be used as a time base for anything requiring fixed phase.
