---
title: "KiCad Custom DRC Rules: Teach the Board Checker About Your Sensor Design"
date: 2026-07-31
track: cad-3dprint
summary: "KiCad's .kicad_dru file lets you write design rules the built-in DRC can't express — extra clearance for a high-voltage net, a minimum annular ring, copper kept off the board edge. The S-expression syntax, the A/B query language, and what KiCad 9 added to the engine."
reading_time: 5
tags: [kicad, pcb, drc, design-rules, hardware, electronics]
sources:
  - title: "KiCad PCB Editor documentation — Custom Design Rules (v9.0)"
    url: "https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html"
  - title: "KiCad — Version 9.0.0 Released (Feb 20, 2025)"
    url: "https://www.kicad.org/blog/2025/02/Version-9.0.0-Released/"
  - title: "labtroll/KiCad-DesignRules — real working JLCPCB.kicad_dru"
    url: "https://github.com/labtroll/KiCad-DesignRules/blob/main/JLCPCB/JLCPCB.kicad_dru"
  - title: "Mastering KiCad PCB Design Rules: A Practical Guide & Template — e-mbed"
    url: "https://e-mbed.com/kicaddesignrules/"
  - title: "KiCad Custom DRC Rules — Maskset"
    url: "https://www.maskset.net/blog/2023/10/30/kicad-custom-drc-rules/"
---

The default DRC in KiCad checks the rules that apply to *every* board — minimum clearance, track width, hole sizes. But your sensor board has requirements the generic checker doesn't know about: the mains-referenced section needs a full millimeter of isolation, the vias on the analog front-end need a healthy annular ring, and no copper should stray near the routed edge where the depaneling bit runs. **Custom design rules** let you encode exactly those constraints so the DRC catches your specific mistakes, not just the universal ones.

Worth being precise about the history: the `.kicad_dru` custom-rules feature is *not* new — it shipped in KiCad 7 (2023). **KiCad 9**, released February 20, 2025, refined the engine around it: a new **creepage** constraint for high-voltage spacing, **component classes** you can test in rules, and user-defined DRC/ERC violation markers via `${DRC_ERROR <title>}` text variables. The syntax below works from 7 onward and is what you'll use daily on 9.

## Where the rules live and how they read

You write custom rules in **File → Board Setup → Design Rules → Custom Rules** — a text pane with syntax highlighting and autocomplete — and KiCad persists them in a sibling file named `<project>.kicad_dru`, which you can also hand-edit or drop in as a shared vendor ruleset.

The grammar is S-expressions. The file opens with a version header, and each rule names itself, optionally scopes to layers or overrides severity, carries one or more constraints, and gates itself with an optional condition:

```
(version 1)

(rule "name"
  (severity error)
  (constraint <type> (min ...) (max ...))
  (condition "<expression>"))
```

Constraints cover far more than the Board Setup dialog exposes: `clearance`, `track_width`, `annular_width`, `hole_size`, `hole_to_hole`, `edge_clearance`, `courtyard_clearance`, `via_diameter`, `diff_pair_gap`, `length`, `skew`, `disallow`, and the new `creepage`. The **condition** is where the power lives. Two objects are under test, `A` and `B` (in a clearance check, B is the other object), with properties like `A.NetClass`, `A.Layer`, `A.Type` (`'track'`, `'via'`, `'pad'`, `'zone'`), and helper functions `A.isPlated()`, `A.insideCourtyard('U1')`, `A.existsOnLayer('F.Cu')`. Operators are the C-style `==`, `!=`, `&&`, `||`, `!`, and string literals use single quotes.

## Three rules you'll actually want

Give a high-voltage net class a full millimeter to everything that isn't also HV:

```
(rule "HV isolation"
  (severity error)
  (condition "A.NetClass == 'HV' && B.NetClass != 'HV'")
  (constraint clearance (min 1mm)))
```

Keep copper 0.3 mm clear of the routed board outline:

```
(rule "Edge to track clearance"
  (condition "A.Type == 'track'")
  (constraint edge_clearance (min 0.3mm)))
```

Enforce a minimum annular ring on plated vias and through-holes on the outer layers:

```
(rule "Annular ring (via and PTH)"
  (layer outer)
  (condition "A.isPlated()")
  (constraint annular_width (min 0.075mm)))
```

Each of these becomes a first-class DRC violation with your rule name attached, so when it trips you know *why* — "HV isolation" is a lot more actionable than a generic clearance error. Assign your mains nets to an `HV` net class in Board Setup, run DRC, and any 0.4 mm gap you accidentally routed lights up red.

The reason this matters more than it looks: a custom rule is *documentation the tool enforces*. Six months later, when you or a collaborator re-routes that board, the isolation requirement isn't a note in a datasheet anyone can ignore — it's a rule that fails the check. KiCad 9's `creepage` constraint takes this further for real high-voltage work, distinguishing surface-distance from straight-line clearance.

**Try next:** On your current board, create an `HV` net class, assign your highest-voltage nets to it, and add the HV isolation rule above with `(min 1mm)`. Run DRC and deliberately drag a trace to 0.5 mm from an HV pad to watch it fail with your rule's name. Then commit the `.kicad_dru` alongside your project so the constraint travels with the design.
