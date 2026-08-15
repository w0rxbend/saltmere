---
title: "KiCad 9 and ngspice: simulating a sensor's analog front-end before routing"
date: 2026-07-30
track: cad-3dprint
summary: "KiCad's built-in ngspice simulator proves an RC filter or bias network from the same schematic that becomes the PCB, with no separate SPICE tool and no re-drawing. This article covers model assignment in KiCad 9 and a worked RC low-pass for a noisy analog sensor line, simulated in transient and AC."
reading_time: 5
tags: [kicad, ngspice, spice, simulation, analog, sensors]
sources:
  - title: "SPICE Simulation — KiCad project site"
    url: "https://www.kicad.org/discover/spice/"
  - title: "Simulator — KiCad 9 Eeschema documentation"
    url: "https://docs.kicad.org/9.0/en/eeschema/eeschema.html#simulator"
  - title: "KiCad Eeschema as GUI for ngspice — ngspice.sourceforge.io"
    url: "https://ngspice.sourceforge.io/ngspice-eeschema.html"
  - title: "KiCad 9.0 Release Notes"
    url: "https://www.kicad.org/blog/2025/02/KiCad-9.0.0-Release/"
---

**Gist.** A schematic drawing carries no information about analog behaviour: whether an analog-to-digital converter (ADC) input rings, whether a bias divider sags under load, whether a resistor–capacitor (RC) filter has the corner frequency the design assumed. The **ngspice** engine is embedded in Eeschema and ships with KiCad **9** (released February 2025), so the same sheet that becomes the printed circuit board (PCB) can be run as a circuit before any footprint is placed. The cost is model fidelity: ngspice simulates only what its models describe, so every symbol must be bound to a model by hand, and a poor third-party model produces a confidently wrong plot.

## The binding step: symbols carry no behaviour until a model is assigned

A schematic symbol is a drawing with pins and a reference designator. ngspice requires a netlist of *devices*. The bridge is the **Simulation Model** dialog, reached from the *Simulation Model…* button in a symbol's properties. The invariant that governs the whole workflow: **a symbol with no assigned simulation model is silently omitted from the netlist handed to ngspice.** The simulator does not error; it solves a smaller circuit than the one on screen. A divider whose lower leg was never bound simulates as an open circuit, and the output node reads full supply.

Two assignment paths exist.

- **Built-in device models.** Resistor, capacitor, inductor, diode, bipolar junction transistor (BJT), metal-oxide-semiconductor field-effect transistor (MOSFET), and independent or controlled sources. The dialog asks for the device type and the value. This covers most component-level analog front-end work.
- **External SPICE models.** The dialog is pointed at a `.lib` or `.sub` file — typically a manufacturer's model for an operational amplifier, voltage reference, or transistor — and the symbol's pins are mapped onto the model's node order. KiCad ships standard and PSpice symbol libraries but **does not ship third-party device models**; those are downloaded from the part's own page. Pin-to-node mapping is the failure-prone half: the symbol's pin numbering and the subcircuit's argument order are independent, and a mismatch produces a circuit that solves without complaint.

Two further elements are required before any analysis converges. A **source** supplies stimulus (`VDC` for an operating point or direct-current sweep, `VSIN` for transient excitation). A **ground** establishes the reference node, which in SPICE is literally node `0`; KiCad's GND symbol maps to it. Every node in the circuit must have a direct-current path to node `0`, or the solver has no reference for the matrix it is inverting.

Finally the analysis itself is chosen either in the simulator's *Settings* dialog or written as a text directive placed on the sheet. The directive syntax is plain ngspice: `.tran`, `.ac`, `.dc`, `.op`.

## Worked example: a first-order low-pass on a noisy sensor line

Consider an analog gas or light sensor whose real information is sub-hertz, riding on mains-frequency and switching noise. A first-order RC low-pass ahead of the ADC is the standard remedy. The corner frequency is

    f_c = 1 / (2 π R C)

Choosing `R = 10 kΩ` and `C = 1 µF` places `f_c ≈ 16 Hz`. The design question — does that attenuate 50 Hz hum while passing the signal — is answered by two analyses on the same schematic.

The topology is `V1` → `R1` (10 kΩ) → node `out` → `C1` (1 µF) → GND, with the built-in R and C models assigned and a sine model on `V1`. The transient directives:

```spice
* Stimulus: 1 V amplitude, 50 Hz hum, offset to mid-supply
V1 in 0 DC 1.65 SIN(1.65 1 50)

* Transient: 0.1 ms step, 100 ms of simulated time
.tran 0.1m 100m
```

Probing `out` against `in` shows the 50 Hz swing arriving at `out` reduced in amplitude and shifted in phase. **Transient analysis shows the response at one frequency**; it does not report the corner. Reading the corner requires the frequency-domain sweep, which needs a different source specification:

```spice
V1 in 0 DC 0 AC 1        ; unit AC magnitude: gain is read directly off the plot
.ac dec 20 1 100k        ; 1 Hz to 100 kHz, 20 points per decade
```

The `AC 1` magnitude matters because the plotted curve is then the transfer function itself rather than a scaled copy of it. KiCad plots gain against frequency on a logarithmic axis. At the `≈16 Hz` corner the response sits at the **`−3 dB`** point by definition. At 50 Hz — roughly three times the corner — the first-order roll-off has already taken the response to approximately **`−10 dB`**. Adjusting `R1` or `C1` in the schematic and re-running is a matter of seconds; adjusting them after fabrication is a board revision.

The `.tran` and `.ac` directives are mutually exclusive within one run: the analysis run is the one described by the directive in force, so the transient text must be commented out or removed when sweeping.

## What co-locating the simulation buys, and what it does not

The argument for simulating inside KiCad is not that its ngspice is superior to a standalone ngspice — it is the same engine. The property is that **the simulated netlist and the fabricated netlist are derived from one schematic**, so there is no second model to keep synchronised. A value changed for the simulation is the value that reaches the bill of materials and the layout. The classic divergence — the simulation used 4.7 kΩ, the assembled board received 47 kΩ — has no place to occur.

The limits are structural rather than incidental. ngspice solves lumped-element analog and mixed-signal circuits at the component level. It does not address digital timing closure, radio-frequency electromagnetic behaviour, or signal integrity work requiring IBIS or S-parameter models. And results inherit model quality directly: an inaccurate operational-amplifier `.sub` yields an inaccurate plot with no indication that anything is wrong. Within its scope — filters, dividers, bias networks, reference stability, output swing — it converts a class of post-fabrication surprises into a check measured in minutes.

An instructive extension: cascading a second identical RC stage after the first and re-running the `.ac` sweep changes the asymptotic roll-off from first-order `−20 dB/decade` to second-order `−40 dB/decade`, visible directly as a change of slope on the plot. Note that the two stages are not isolated — the second stage loads the first — so the cascaded corner does not equal the single-stage corner.

## Pitfalls

- **A symbol without an assigned simulation model disappears from the netlist.** No warning is raised; the analysis converges on a circuit missing that component, so a shunt leg reads as an open and a series leg as a short in the plotted result.
- **A node with no direct-current path to node `0` leaves the nodal matrix singular and the analysis fails.** SPICE requires a ground reference; a capacitively coupled island with no bias resistor leaves the solver without one.
- **A `VSIN` source with no `AC` magnitude produces a meaningless `.ac` plot.** The transient sine parameters and the AC sweep magnitude are separate fields on the same source; a sweep run against a source declared only with `SIN(...)` does not describe the intended stimulus.
- **Mis-mapped pins on an external `.sub` model simulate silently.** Subcircuit node order is independent of symbol pin numbering, so swapping an operational amplifier's inverting and non-inverting inputs yields a circuit that solves and is wrong.
- **Transient results at one excitation frequency are not a frequency response.** Attenuation observed on a 50 Hz `.tran` says nothing about the corner or the roll-off slope; only `.ac` reports those.
- **Third-party models are not installed with KiCad.** The shipped standard and PSpice libraries contain symbols; the corresponding device models must be obtained separately from the manufacturer.
