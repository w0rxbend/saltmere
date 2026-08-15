---
title: "atopile: defining PCBs in code with the .ato language"
date: 2026-08-13
track: cad-3dprint
summary: "atopile is a compiler for electronics: circuits are described in a declarative .ato language with units, tolerances and equations, the compiler solves for real passive values, selects orderable parts, and emits a KiCad board for layout. Covers the language model, the build loop, and the maturity of the 0.15.x toolchain."
reading_time: 6
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

**Gist.** A schematic records component values but not the requirements those values were chosen to meet, so a substitution cannot be checked mechanically and a diff of two revisions carries no design intent. atopile replaces the drawn schematic with a declarative source language, `.ato`, in which the designer states physical constraints — voltages, tolerances, resistance ranges — and a constraint solver derives concrete passive values and orderable part numbers, emitting a KiCad printed circuit board (PCB) for interactive layout. The cost is a pre-1.0 toolchain whose syntax and part-selection backend are both moving: part picking is centred on one distributor, and features arrive behind explicit experiment gates.

## The language: interfaces, units, tolerances

`.ato` is declarative. It has no conditionals and no functions. Objects are created with `new`, joined with `~` (equivalence between two interfaces) and `~>` (a series path *through* a bridgeable element, so the left terminal and right terminal are inferred rather than named). Values are physical quantities carrying units and tolerance, written directly in the source: `3.3V +/- 5%`, `100nF +/- 20%`, `100kHz to 400kHz`.

The unit of composition is the **interface**: a named bundle of nets with an agreed structure. `ElectricPower` carries `hv` and `lv`; `ElectricLogic` carries a line plus its reference; `I2C` and `SPI` bundle their respective signal groups. Connecting a sensor's I2C bus is therefore a single `~` between two `I2C` interfaces rather than four individual net assignments, and **a structural mismatch between the two sides is a compile-time error rather than a mis-wired net discovered at bring-up**.

The following module is complete — the battery-voltage divider that a battery-powered ESP32 sensor node requires in order to read pack voltage on an analogue-to-digital converter (ADC) input:

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

What the source omits is the resistance values. The three `assert` statements express the requirements the circuit must meet — the pack voltage range, the ceiling the ADC input must not exceed, and a lower bound on total resistance that limits the quiescent drain on the pack — and **the compiler solves for the passives**, selecting concrete E-series values, and part numbers stocked at the distributor, that satisfy every constraint. The solve is over intervals, not points: each candidate resistor contributes its tolerance band, so a pair is admissible only if **the divider output stays inside the asserted range across the full tolerance stack-up**, not merely at nominal values. Changing the asserted pack range from a 2S to a 3S battery and rebuilding re-solves both resistors.

This is the substantive difference from a drawn schematic. The requirement is stored next to the circuit in machine-checkable form, so an infeasible edit fails the build rather than producing a board that is wrong in a way no tool can detect.

## The build loop

```bash
pip install atopile          # Python 3.14 required for 0.15.x
ato create project           # scaffold: ato.yaml, main.ato, layouts/
ato build                    # solve, pick parts, emit netlist + KiCad board
```

`ato build` performs three distinct stages: constraint validation, part selection against distributor stock, and emission of a `.kicad_pcb` file together with a bill of materials (BOM) carrying orderable part numbers. **Layout remains interactive and manual** — placement and routing happen in KiCad, against the netlist the compiler emits. The emitted project is an ordinary KiCad project, so [custom design rule check (DRC) rules](/articles/cad-3dprint/2026-07-31-kicad-9-custom-drc-rules) and [IPC-API scripting with kipy](/articles/cad-3dprint/2026-08-10-kicad-9-ipc-api-kipy) apply downstream without modification.

Reuse operates through a package registry at packages.atopile.io. `ato add <package>` installs a circuit block — a regulator, a sensor breakout, a microcontroller module — and **the block's own asserts remain in force after installation**, so configuring a low-dropout regulator (LDO) as `ldo.output_voltage = 3.3V +/- 5%` outside the range its internal constraints permit fails the build rather than the board. `ato create part` generates a component definition (pins, footprint, 3D model) from an LCSC/JLCPCB part number. Instantiating a sensor breakout then reduces to three statements: `sensor = new BME280`, `sensor.i2c ~ mcu.i2c`, `sensor.power ~ power_3v3`.

## Maturity of the 0.15.x toolchain

The current release is **v0.15.8** (August 2026) on the Python Package Index (PyPI), MIT-licensed, with roughly 3.6k GitHub stars and a VS Code extension. The core loop — write, solve, build, lay out, order — functions, and shipped boards exist.

The project is pre-1.0 and changes accordingly. The v0.14.0 release of 31 January 2026 is described in its own release notes as a complete core rewrite. A number of features, including the `~>` bridge connection used above, are reachable only after an explicit `#pragma experiment(...)` declaration — the pragma name itself records that the syntax is provisional. **Source written against 0.15.x should therefore be expected to require migration** across minor releases.

Two structural limits apply independently of release cadence. Part selection is oriented towards JLCPCB/LCSC stock, which constrains designs whose supply chain lies elsewhere. The registry is small, so a part it does not cover must be defined by hand — pin mapping transcribed from the datasheet — which is the work the reuse model is meant to eliminate.

The cases where the approach pays are parametric families of small boards, designs rebuilt often, and team work where review of a textual diff is more tractable than review of a rendered schematic. A KiCad-first flow remains preferable for one-off boards, for analogue work requiring manual control of every value, and for designs whose parts are not stocked at LCSC.

## Pitfalls

- **A build that reports no solution names the constraint set, not the culprit.** Asserts interact: a total-resistance floor combined with an output-ceiling assert can be jointly infeasible even though each is individually satisfiable.
- **Nominal-value reasoning misleads.** A divider verified by hand at nominal values can still fail the solve, because admissibility is evaluated across each part's tolerance band, not at its centre.
- **Omitting a `#pragma experiment(...)` line makes valid syntax a parse error.** The `~>` bridge connection requires `BRIDGE_CONNECT` to be declared before use.
- **Upgrading across a minor release can break a working project.** 0.14.0 was a core rewrite, and features behind `#pragma experiment(...)` are provisional by construction; pin the atopile version in the project rather than tracking the latest release.
- **0.15.x requires Python 3.14.** An install into an older interpreter fails at dependency resolution rather than at build time.
- **Part availability is a build input.** Because selection runs against distributor stock, a rebuild that was reproducible last month can select different part numbers, changing the BOM without any source change.
- **Manually edited nets in KiCad are not source.** The board is regenerated from the netlist, so connectivity added in the layout editor rather than in `.ato` is outside the solver's knowledge and will not survive as design intent.
