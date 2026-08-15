---
title: "Length-Tuning Differential Pairs in KiCad 9: Meanders, Skew, and the Tuning Tools"
date: 2026-08-15
track: cad-3dprint
summary: "On a USB or Ethernet pair, a fraction of a millimetre of length mismatch becomes picoseconds of skew that the receiver reads as jitter. KiCad 9.0.9 ships three tuning tools — single track, differential-pair length, and differential-pair skew — driven by net-class width and gap. This article covers net-class setup and correct meander placement."
reading_time: 6
tags: [kicad, pcb, high-speed, differential-pair, length-tuning, signal-integrity]
sources:
  - title: "KiCad 9.0 PCB Editor manual — Length tuning & Differential pairs"
    url: "https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html"
  - title: "KiCad 9.0.9 Release"
    url: "https://www.kicad.org/blog/2026/04/KiCad-9.0.9-Release/"
  - title: "How to Route Differential Pairs in KiCad — Sierra Circuits"
    url: "https://www.protoexpress.com/blog/how-to-route-differential-pairs-in-kicad/"
  - title: "How to Route Differential Pairs in KiCad (for USB) — DigiKey"
    url: "https://www.digikey.com/en/maker/projects/how-to-route-differential-pairs-in-kicad-for-usb/45b99011f5d34879ae1831dce1f13e93"
  - title: "Differential Pair Length Matching Guidelines — Cadence"
    url: "https://resources.pcb.cadence.com/blog/2025-differential-pair-length-matching-guidelines"
---

**Gist.** A differential receiver recovers data from the difference between two conductors, and that subtraction cancels common-mode noise only when both edges arrive together; unequal trace lengths convert differential energy into common-mode energy, which appears as jitter at the far end and as radiated emission. KiCad 9 corrects the mismatch by inserting **meanders** — serpentine detours that add controlled path length — under three separate tools: single-track length, differential-pair length, and differential-pair skew. The cost is electrical: the parallel runs of a serpentine couple to themselves and every corner is an impedance discontinuity, so a length-matched trace is not equivalent to a straight trace of the same length.

## The quantity being matched

Propagation delay, not distance, is what the receiver observes. On typical FR4 a signal propagates at roughly **6 ps/mm** (about 150 ps per inch), so **1 mm of mismatch on a USB 3 or PCIe pair costs about 6 ps** — a meaningful fraction of the bit period at those rates. Two distinct mismatches exist and they have different consequences:

- **Intra-pair skew** is the length difference between the P and N conductors of one pair. This is the mismatch that breaks the subtraction and produces mode conversion.
- **Inter-pair mismatch** is the difference in total length between one pair and another pair in the same group — the four lanes of a QSPI bus, or the byte lanes of DDR. This does not break the subtraction inside any pair; it misaligns pairs relative to each other.

The tool chosen must match the mismatch being corrected.

## Net class first: length tuning does not set impedance

Differential impedance is governed by trace **width**, the **gap** between the two conductors, and the dielectric stackup. Length has no part in it. Setting those geometry values is a prerequisite, not a follow-up.

The controls live under **File → Board Setup → Design Rules → Net Classes**. Each net-class row carries dedicated pair columns — **Diff Pair Width** and **Diff Pair Gap** — alongside the single-ended Clearance, Track Width and Via Size. High-speed nets are assigned to the class, and width and gap are set to reach the target differential impedance: **90 Ω for USB**, **100 Ω for Ethernet and PCIe**, **100 Ω for HDMI TMDS pairs**.

Those values need not be guessed. KiCad ships the **PCB Calculator** as a standalone tool; its **Transmission Line → Coupled Microstrip** (or stripline) page solves width and gap for a target differential impedance given layer thickness and dielectric constant. The stackup is entered under **Board Setup → Physical Stackup**, and the resulting width and gap are transcribed into the net class.

```text
Board Setup → Design Rules → Net Classes
  Net Class: USB_HS
    Clearance ........... 0.20 mm
    Track Width ......... 0.25 mm
    Diff Pair Width ..... 0.25 mm   ← from PCB Calculator, ~90 Ω
    Diff Pair Gap ....... 0.15 mm   ← from PCB Calculator
  Assign nets: /USB_D+  /USB_D-     (names must share a base + P/N suffix)
```

The router enforces one naming invariant: a pair must be **named with matching + / − (or _P / _N) suffixes** on a shared base name so that KiCad identifies the two nets as partners. Nets that violate this are treated as unrelated single-ended nets, and **the differential-pair router finds no partner to couple to** — the fix is renaming in the schematic, not in the board.

## The three tools and what each one changes

Routing begins with **Route → Route Differential Pair** (default hotkey **6**). Clicking either pad of the pair lays both traces at the net-class width and gap, keeping them coupled around corners. The pair is routed to approximately equal length by hand; the tuning tools adjust the residual, not gross geography.

| Tool (Route menu) | Default hotkey | Quantity adjusted |
|---|---|---|
| Tune Length of a Single Track | 7 | One net to a target length (clock, DDR byte lane) |
| Tune Length of a Differential Pair | 8 | The **pair's total length** up to a target (inter-pair matching) |
| Tune Skew of a Differential Pair | 9 | The **length difference between P and N** within the pair (intra-pair skew) |

The last two are the pair routinely confused. **Tune Length** lengthens the *whole pair*, both conductors together, and applies when this pair must match the length of *another* pair. **Tune Skew** adds a meander to whichever of P or N is shorter, leaving the pair total roughly incidental, and equalises the two conductors. Since intra-pair skew is the mismatch that produces mode conversion, skew tuning applies to every pair; length tuning applies only where a group must track together.

With the tool active, hovering over a track or pair produces a live length read-out and draws the candidate meander as the pointer moves; a click commits it. Target and geometry are set in **Length Tuning Settings**, reachable by right-click while the tool is active. The load-bearing fields:

- **Tune from / to** and **Target length** — the length being matched to, sourced from the design rules or entered directly.
- **Min amplitude** / **Max amplitude** — the height of the meander humps. Larger amplitude yields fewer, taller humps for the same added length.
- **Spacing** — the gap between adjacent meander segments. A common guideline is **≥ 3× the trace width**, limiting the coupling of the meander back onto itself.
- **Corner style** — Chamfer or Rounded (Fillet), with a rounding percentage. Rounded corners present a gentler impedance discontinuity.

Amplitude can be raised or lowered during placement to fit the meander into available board area. KiCad reports running length against the target in a heads-up gauge whose colour indicates whether the current length falls inside the target window.

Two placement consequences follow from the mechanism. Skew correction is best placed **close to the source of the mismatch** — immediately after a connector or a via pair rather than at the far end — because the two conductors are uncoupled and mismatched over the intervening span. And matching should stop at the tolerance the interface requires: additional meandering past that point adds coupling and corners without removing skew that matters.

Version note: this description is written against **KiCad 9.0.9**, a 9.0.x bugfix release.

## Pitfalls

- **Nets named without matching suffixes**: clicking a pad produces a single track rather than a coupled pair, because KiCad cannot identify a partner net for the one under the pointer.
- **Tune Length used where Tune Skew was needed**: the pair's total length reaches its target while P and N remain unequal, so the mode conversion that caused the jitter is untouched.
- **Length tuned before width and gap are set in the net class**: the traces carry the wrong differential impedance, and re-routing at the corrected width discards every meander already placed.
- **Meander spacing below roughly 3× the trace width**: adjacent serpentine segments couple to each other, so the added electrical delay departs from the geometric length KiCad reports.
- **Skew meander placed at the far end of the run**: the pair propagates skewed across the whole intervening length, and the correction restores equal total length without restoring edge alignment along that span.
- **Over-tuning past the interface tolerance**: each additional hump contributes corners and self-coupling, degrading the impedance profile in exchange for skew reduction the receiver does not require.
- **Large meander amplitude chosen to save board area**: taller humps place longer parallel runs adjacent to one another, increasing the self-coupling the 3W spacing guideline exists to bound.
