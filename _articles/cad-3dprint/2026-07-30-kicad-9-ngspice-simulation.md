---
title: "KiCad 9 + ngspice: simulate your sensor's analog front-end before you route it"
date: 2026-07-30
track: cad-3dprint
summary: "KiCad's built-in ngspice simulator lets you prove an RC filter or bias network works from the same schematic you'll turn into a PCB — no separate SPICE tool, no re-drawing. Here's how model assignment works in KiCad 9, and a worked RC low-pass for cleaning up a noisy analog sensor signal, simulated in transient and AC."
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

Half of a hobby sensor board's problems are analog and invisible on a schematic: an ADC input that rings, a bias divider that sags under load, an RC filter with the wrong corner frequency so it either passes noise or smears your signal. You *could* find these after fabrication with a scope and a reflow rework. Or you could simulate the front-end in the same tool you're already drawing the schematic in. Since KiCad 7 the **ngspice** engine has been built into Eeschema, and KiCad **9** (released February 2025) refined the simulator UI further — so "will this filter actually work?" is answerable before you place a single footprint.

## The one concept: symbols need simulation models

A schematic symbol is a drawing; ngspice needs to know the *component behind it*. The bridge is the **Simulation Model** dialog (in a symbol's properties, the *Simulation Model…* button). For passives it's trivial — a resistor symbol gets an "R" model and you just confirm its value — but this is the step people miss, because a symbol with no model is silently ignored by the simulator. Two paths:

- **Built-in device models:** resistor, capacitor, inductor, diode, BJT/MOSFET, independent/controlled sources. Pick the type, fill the value. This covers most analog front-end work.
- **External SPICE models:** point the dialog at a `.lib`/`.sub` file from a manufacturer (op-amps, references, transistors) and map the symbol's pins to the model's nodes. KiCad ships standard and PSpice symbol libraries but *not* third-party device models — you download those from the part's page.

You also need **sources** (a `VDC` or `VSIN` for stimulus) and a **ground** (node `0` in SPICE terms — KiCad's GND symbol). Then you add a **simulation directive**: a text directive on the sheet, or the analysis you choose in the simulator's *Settings* dialog. The directive is plain ngspice: `.tran`, `.ac`, `.dc`, `.op`.

## Worked example: an RC low-pass for a noisy sensor line

Say an analog gas or light sensor outputs a slow signal (sub-Hz of real information) riding on mains-frequency and switching noise. You want a first-order RC low-pass feeding the ADC. Pick a corner around 16 Hz: with `R = 10 kΩ`, `f_c = 1 / (2πRC)` gives `C ≈ 1 µF`. Does it actually knock down 50 Hz hum while passing the signal? Simulate it.

Draw: `V1` (a `VSIN`) → `R1` (10 k) → node `out` → `C1` (1 µF) → GND. Assign the built-in R and C models, give `V1` a sine model, and drop these directives on the sheet:

```spice
* Stimulus: 1 V, 50 Hz "hum" we want to attenuate, small DC offset
V1 in 0 DC 1.65 SIN(1.65 1 50)

* Transient: watch the filtered output settle over 100 ms
.tran 0.1m 100m

* (swap in for a frequency sweep instead — see below)
* .ac dec 20 1 100k
```

Run the transient analysis and probe node `out` against `in`. You'll see the 50 Hz swing on `in` arrive at `out` visibly shrunk and phase-shifted — the filter working, quantitatively, on your screen. To read the *corner* directly, swap to the AC sweep:

```spice
V1 in 0 DC 0 AC 1        * AC magnitude 1 for a frequency-response sweep
.ac dec 20 1 100k        * 1 Hz to 100 kHz, 20 points per decade
```

Now probe `out` and KiCad plots gain vs. frequency on a log axis. Drop a cursor at 50 Hz: you'll read roughly `−10 dB` (the 50 Hz tone is already well down the RC's roll-off), and at the `≈16 Hz` corner you'll see the classic `−3 dB` point. If the corner is too high (noise leaks through) or too low (your real signal gets attenuated too), you change `R1`/`C1` in the schematic and re-run — seconds, not a board respin.

## Why simulate in KiCad specifically

The value isn't that ngspice is better than standalone SPICE — it's that the simulation runs on the **exact schematic you'll turn into a PCB**, so there's no separate model to keep in sync and drift out of date. Change a resistor for the simulation and that same value carries into your BOM and layout. It closes the loop that usually breaks: "the sim used 4.7 k but the board got 47 k." KiCad 9's simulator also improved plotting and measurement, so reading a `−3 dB` point or comparing two probes is less fiddly than it was.

Its limits are honest: ngspice is excellent for analog and mixed-signal at the component level, not for digital timing, RF electromagnetics, or anything needing IBIS/S-parameters. And a simulation is only as good as its models — a garbage op-amp `.sub` gives garbage results. But for the bread-and-butter of a sensor board — filters, dividers, bias networks, reference stability, does-this-op-amp-swing-rail-to-rail — it turns a class of post-fabrication surprises into a two-minute check.

**Try next:** Add a second RC stage after the first (same 10 k / 1 µF) to make a two-pole filter, re-run the `.ac` sweep, and compare the roll-off slopes — you'll watch first-order `−20 dB/decade` become second-order `−40 dB/decade` on the plot, and can decide whether the extra part is worth the sharper cutoff for your sensor.
