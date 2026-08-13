---
title: "atopile: defining PCBs in code with the .ato language"
date: 2026-08-13
track: cad-3dprint
summary: "atopile is a compiler for electronics: you describe circuits in a declarative .ato language with units, tolerances, and equations, and it solves for real passives, picks orderable parts, and emits a KiCad board for layout. Here's the workflow, a runnable voltage-divider module, and an honest look at how mature the 0.15.x toolchain actually is."
reading_time: 5
tags: [atopile, pcb, kicad, code-cad, electronics]
sources:
  - title: "atopile/atopile (GitHub)"
    url: "https://github.com/atopile/atopile"
  - title: "atopile on PyPI"
    url: "https://pypi.org/project/atopile/"
  - title: "atopile — Design electronics with code"
    url: "https://atopile.io/"
  - title: "Intro to Atopile — Eigenlucy"
    url: "https://eigenlucy.com/projects/atopile_guide/"
  - title: "Show HN: Atopile — Design circuit boards with code (Hacker News)"
    url: "https://news.ycombinator.com/item?id=39263854"
---

Schematics are pictures, and pictures don't diff, don't compose, and don't carry design intent. The resistor says "10k"; it doesn't say *why* 10k, what tolerance the circuit actually needs, or which constraint breaks if you swap it. **atopile** attacks this the way [build123d](/articles/cad-3dprint/2026-07-27-build123d-code-cad) attacks mechanical CAD: make the source of truth code. You write `.ato` files describing modules, interfaces, and constraints; the compiler solves the constraints, picks real orderable components, and hands KiCad a board to lay out. Current release: **v0.15.8** (August 2026) on PyPI, MIT-licensed, ~3.4k GitHub stars, with a VS Code extension and a package registry at packages.atopile.io.

## The language: interfaces, units, tolerances

`.ato` is declarative — no if/else, no functions. You instantiate things with `new`, connect them with `~` (equivalence) and `~>` (series path through a bridgeable element), and state requirements as physical quantities with tolerances. Values are first-class units: `3.3V +/- 5%`, `100nF +/- 20%`, `100kHz to 400kHz`. Interfaces like `ElectricPower`, `ElectricLogic`, `I2C`, and `SPI` bundle related nets, so connecting a sensor's I2C bus is one line, not four.

Here's a complete module — the battery-voltage divider every ESP32 air-quality node ends up needing:

```ato
#pragma experiment("BRIDGE_CONNECT")
import Resistor
import ElectricPower
import ElectricSignal

module BatteryDivider:
    """Scale a 2S pack voltage down to an ESP32 ADC's range."""
    power = new ElectricPower
    output = new ElectricSignal

    r_top = new Resistor
    r_bottom = new Resistor
    r_top.package = "0402"
    r_bottom.package = "0402"

    # Series path: pack+ -> r_top -> tap -> r_bottom -> pack-
    power.hv ~> r_top ~> output.line
    output.line ~> r_bottom ~> power.lv
    output.reference ~ power

    # Design intent, as equations the solver must satisfy
    assert power.voltage within 6.0V to 8.4V
    assert power.voltage * r_bottom.resistance /
        (r_top.resistance + r_bottom.resistance) within 0V to 3.1V
    assert r_top.resistance + r_bottom.resistance within 90kohm to 500kohm
```

Notice what's *absent*: resistor values. The asserts state the requirements — never exceed the ADC's 3.1 V, keep the total resistance high enough to spare the battery — and the compiler **solves for the passives**, picking concrete E-series values (and actual JLCPCB-stocked part numbers) that satisfy every constraint including tolerance stack-up. Change the pack to 3S and rebuild: the resistors re-solve. That's the qualitative jump over drawing schematics — the "why" is machine-checked, like a type system for electronics.

## The workflow: compile to a KiCad board

```bash
pip install atopile          # Python 3.14 required for 0.15.x
ato create project           # scaffold: ato.yaml, main.ato, layouts/
ato build                    # solve, pick parts, emit netlist + KiCad board
```

`ato build` validates constraints, runs part selection against distributor stock, and produces/updates a `.kicad_pcb` plus a BOM with orderable part numbers. Layout stays interactive: you route in KiCad as usual, and atopile preserves your placement and routing across rebuilds, syncing netlist changes into the board. It slots cleanly in front of everything already covered here — the board it emits is a normal KiCad 9 project, so [custom DRC rules](/articles/cad-3dprint/2026-07-31-kicad-9-custom-drc-rules) and [IPC-API scripting with kipy](/articles/cad-3dprint/2026-08-10-kicad-9-ipc-api-kipy) apply unchanged downstream.

Reuse is the other half of the pitch. `ato add <package>` pulls validated circuit blocks — regulators, sensor breakouts, MCU modules — from the registry, each shipping with its own constraints. Import an LDO package and configure it like a library call (`ldo.output_voltage = 3.3V +/- 5%`); the block's internal asserts still hold, so a misconfiguration fails at compile time, not at bring-up. `ato create part` generates a component definition (pins, footprint, 3D model) straight from an LCSC/JLCPCB part number. An I2C sensor breakout is then genuinely three lines: `sensor = new BME280`, `sensor.i2c ~ mcu.i2c`, `sensor.power ~ power_3v3`.

## Honest maturity assessment

atopile is pre-1.0 and behaves like it. The good: the core loop — write, solve, build, lay out in KiCad, order from the BOM — works, real boards ship on it, and the compiler catching a violated voltage constraint before fab is a genuinely new safety net. The team moves fast, which is also the bad news: the January 2026 0.14 release was a "complete core rewrite," useful features hide behind `#pragma experiment(...)` gates, and the docs advertise a 0.16 with a revised syntax and a browser-based workspace — so expect code you write today to need migration. Part picking is deeply JLCPCB/LCSC-centric; fine for hobby and prototype runs, limiting if your supply chain isn't. And the ecosystem is small: when the registry lacks your part, you're writing the pin mapping yourself, which is where the "software-like reuse" story temporarily deflates back into datasheet transcription.

Where it clearly wins today: parametric families of small boards (a sensor node per gas type, per form factor), anything you rebuild often, and team designs where review-by-diff beats review-by-screenshot. Where KiCad-first still wins: one-off boards, heavy analog work where you want full manual control of every value, and anything whose parts live outside LCSC.

**Try next:** `pip install atopile`, scaffold a project, and port your simplest real circuit — a divider or an LED driver — writing only the constraints and letting the solver pick values; then run `ato build` twice with different supply voltages and diff the BOMs to see the solver earn its keep.
