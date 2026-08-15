---
title: "Length-Tuning Differential Pairs in KiCad 9: Meanders, Skew, and the Tuning Tool"
date: 2026-08-15
track: cad-3dprint
summary: "On a USB or Ethernet pair, a fraction of a millimetre of length mismatch turns into picoseconds of skew that the receiver reads as jitter. KiCad 9.0.9 ships three purpose-built tuning tools — single track, differential-pair length, and differential-pair skew — driven by net-class width/gap. Here is how to set the net class and route the meanders correctly."
reading_time: 6
tags: [kicad, pcb, high-speed, differential-pair, length-tuning, signal-integrity]
sources:
  - title: "KiCad 9.0 PCB Editor manual — Length tuning & Differential pairs"
    url: "https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html"
  - title: "KiCad 9.0.9 Release (final 9.0 bugfix, 28 Apr 2026)"
    url: "https://www.kicad.org/blog/2026/04/KiCad-9.0.9-Release/"
  - title: "How to Route Differential Pairs in KiCad — Sierra Circuits"
    url: "https://www.protoexpress.com/blog/how-to-route-differential-pairs-in-kicad/"
  - title: "How to Route Differential Pairs in KiCad (for USB) — DigiKey"
    url: "https://www.digikey.com/en/maker/projects/how-to-route-differential-pairs-in-kicad-for-usb/45b99011f5d34879ae1831dce1f13e93"
  - title: "Differential Pair Length Matching Guidelines — Cadence"
    url: "https://resources.pcb.cadence.com/blog/2025-differential-pair-length-matching-guidelines"
---

A differential receiver does not read either wire on its own — it reads the *difference* between them. That is its superpower: common-mode noise landing on both traces cancels out. But it only cancels if the two edges arrive together. Let one trace run longer than its partner and the "difference" waveform picks up a sliver of common-mode energy at every transition — read by the far end as jitter, and radiated as EMI. On typical FR4 a signal propagates at roughly **6 ps/mm** (about 150 ps per inch), so a mere 1 mm of mismatch on a USB 3 or PCIe pair is already a meaningful fraction of the bit period. Matching length is not cosmetic; it is the job.

KiCad 9 (this is written against **9.0.9**, the final 9.0.x release from 28 April 2026 — the tools are unchanged in KiCad 10) gives you three distinct tuning tools, and the mistake beginners make is reaching for the wrong one. This walks through the net-class setup first, then each tool.

## Set the net class before you route a single track

Length tuning is meaningless without the geometry that sets impedance. A differential pair's impedance is governed by trace **width**, the **gap** between the two traces, and the dielectric stackup — not by length. Fix those first.

Open **File → Board Setup → Design Rules → Net Classes**. Each net class row has dedicated columns for a pair: **Diff Pair Width** and **Diff Pair Gap** (alongside the single-ended Clearance, Track Width, and Via Size). Assign your high-speed nets to the class, then set width and gap to hit the target differential impedance — commonly **90 Ω for USB**, **100 Ω for Ethernet and PCIe**, **100 Ω for HDMI TMDS pairs**.

You do not have to guess those numbers. KiCad ships the **PCB Calculator** as a standalone tool; its **Transmission Line → Coupled Microstrip** (or stripline) page solves width and gap for a target Zdiff given your layer thickness and dielectric constant. Enter your stackup from **Board Setup → Physical Stackup**, read out width/gap, and type them back into the net class.

```text
Board Setup → Design Rules → Net Classes
  Net Class: USB_HS
    Clearance ........... 0.20 mm
    Track Width ......... 0.25 mm
    Diff Pair Width ..... 0.25 mm   ← from PCB Calculator, ~90 Ω
    Diff Pair Gap ....... 0.15 mm   ← from PCB Calculator
  Assign nets: /USB_D+  /USB_D-     (names must share a base + P/N suffix)
```

One rule the router enforces: a pair must be **named with matching + / - (or _P / _N) suffixes** so KiCad recognises the two nets as partners. Rename mismatched nets in the schematic first, or the differential router will not engage.

## Route the pair, then tune the three quantities

With the net class in place, start routing with **Route → Route Differential Pair** (default hotkey **6**). Click either pad of the pair; KiCad lays both traces at the net-class width and gap, holding them coupled around corners. Route the pair to roughly equal length by eye — the tools below are for the final micrometres, not for hauling a trace across the board.

Now there are three separate tools, and choosing correctly is the whole game:

| Tool (Route menu) | Default hotkey | What it fixes |
|---|---|---|
| Tune Length of a Single Track | 7 | One net to a target length (e.g. clock, DDR byte lane) |
| Tune Length of a Differential Pair | 8 | The **pair's total length** up to a target (inter-pair matching) |
| Tune Skew of a Differential Pair | 9 | The **length difference between P and N** within the pair (intra-pair skew) |

The distinction between the last two is the one people get wrong. **Tune Length** makes the *whole pair* longer — you use it when this pair must match the *length of another pair* (inter-pair matching, e.g. the four lanes of a QSPI or the byte lanes of DDR). **Tune Skew** fixes the mismatch *inside a single pair* — it adds a short meander to whichever of P/N is shorter so both wires are equal. Intra-pair skew is the one that converts differential energy to common-mode, so tune skew on every pair; tune length only when a group must track together.

Activate the tool, then hover over the trace or pair. KiCad shows a live length read-out and draws the **meander** (serpentine) as you move the mouse — click to place it. To set the target and the meander geometry, open **Length Tuning Settings** (right-click while the tool is active, or its settings hotkey). The fields that matter:

- **Tune from / to** and **Target length** — the length you are matching to. You can source it from the design rules or type it directly.
- **Min amplitude** / **Max amplitude** — how tall the meander humps are. Bigger amplitude means fewer, taller humps.
- **Spacing** — gap between adjacent meander segments; a common guideline is **≥ 3× the trace width** to limit the meander coupling back onto itself.
- **Corner style** — Chamfer or Rounded (Fillet), with a rounding percentage. Rounded corners are gentler on impedance discontinuity.

While tuning you can bump amplitude up or down live to fit the meander into available space. KiCad reports the running length in a heads-up display and colours the trace when you reach the target window, so you tune until it turns green rather than chasing a number.

The honest caveat: a meander is a compromise. The parallel runs of a serpentine couple to themselves, and every corner is a small impedance bump — so a length-matched trace is not electrically identical to a straight one of the same length. Keep amplitude modest, honour the 3W spacing, place skew corrections **close to the source of the mismatch** (typically right after a connector or a via pair, not at the far end), and do not over-tune: match to the tolerance your interface actually requires and stop. Tighter is not better once you are inside spec.

**Try next:** Wire up a single USB 2.0 pair, set a `USB_HS` net class with ~90 Ω width/gap from the PCB Calculator, route it with hotkey 6, then run **Tune Skew** (9) on it and watch the shorter leg grow a meander until the P/N length delta hits zero in the read-out.
