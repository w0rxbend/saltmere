---
title: "KiCad Custom DRC Rules: Encoding Board-Specific Constraints"
date: 2026-07-31
track: cad-3dprint
summary: "KiCad's .kicad_dru file expresses design rules the built-in design rule check cannot state — extra clearance for a high-voltage net, a minimum annular ring, copper kept off the board edge. The S-expression syntax, the A/B query language, and what KiCad 9 added to the engine."
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

**Gist.** The built-in design rule check (DRC) in KiCad enforces board-wide minima — clearance, track width, hole size — and has no vocabulary for constraints that apply to one net class, one layer, or one region of a particular design. Custom design rules, stored in a `.kicad_dru` file, add that vocabulary: each rule pairs a **constraint** (the numeric requirement) with a **condition** (a boolean expression over the objects under test), so a violation is reported under the rule's own name. The cost is that the constraint now lives in a project-local text file whose expressions are evaluated by the DRC engine rather than checked by a compiler: a condition that never matches produces no error, and no error is indistinguishable from a passing board.

The `.kicad_dru` mechanism is not new — it shipped in KiCad 6. **KiCad 9, released 20 February 2025**, extended the engine around it with a **`creepage` constraint** for high-voltage spacing, **component classes** that rule conditions can test, and user-defined DRC and electrical rule check (ERC) violation markers through `${DRC_ERROR <title>}` text variables. The syntax below applies from version 6 onward.

## Where the rules live

Rules are edited in **File → Board Setup → Design Rules → Custom Rules**, a text pane with syntax highlighting and autocomplete. KiCad persists the contents to a sibling file named `<project>.kicad_dru`. Because that file is plain text, it can be hand-edited outside the editor or dropped in wholesale as a shared vendor ruleset — the referenced `JLCPCB.kicad_dru` is one such fabricator ruleset distributed as a file.

## Grammar

The file is S-expressions. It opens with a version header. Each rule carries a name, optionally a layer scope and a severity override, one or more constraints, and an optional condition:

```
(version 1)

(rule "name"
  (severity error)
  (constraint <type> (min ...) (max ...))
  (condition "<expression>"))
```

The available constraint types cover considerably more than the Board Setup dialog exposes: `clearance`, `track_width`, `annular_width`, `hole_size`, `hole_to_hole`, `edge_clearance`, `courtyard_clearance`, `via_diameter`, `diff_pair_gap`, `length`, `skew`, `disallow`, and `creepage`.

The condition supplies the selectivity. **Two objects are under test, bound to `A` and `B`**; in a clearance check, `B` is the other object of the pair. Conditions read properties such as `A.NetClass`, `A.Layer` and `A.Type` — where the type is a name such as `'Track'`, `'Via'`, `'Pad'` or `'Zone'` — and call helper predicates including `A.isPlated()`, `A.insideCourtyard('U1')` and `A.existsOnLayer('F.Cu')`. Operators follow C syntax: `==`, `!=`, `&&`, `||`, `!`. **String literals use single quotes**, since the whole condition is already delimited by double quotes.

The A/B binding is the part that most often produces a rule that silently does nothing. A condition is evaluated for a pair, and the engine's job is to decide whether the rule applies to that pair; a condition that names only `A` constrains one side and leaves the other unrestricted.

## Three representative rules

A high-voltage net class held a full millimetre from everything not also high-voltage:

```
(rule "HV isolation"
  (severity error)
  (condition "A.NetClass == 'HV' && B.NetClass != 'HV'")
  (constraint clearance (min 1mm)))
```

Copper held 0.3 mm clear of the routed board outline:

```
(rule "Edge to track clearance"
  (condition "A.Type == 'Track'")
  (constraint edge_clearance (min 0.3mm)))
```

A minimum annular ring on plated vias and plated through-holes, restricted to the outer layers:

```
(rule "Annular ring (via and PTH)"
  (layer outer)
  (condition "A.isPlated()")
  (constraint annular_width (min 0.075mm)))
```

Each becomes a first-class DRC violation carrying the rule's name. **The name is the diagnostic**: a marker reading "HV isolation" identifies which requirement was breached, where a generic clearance error identifies only that some minimum was not met.

## Why the file, not a note

A custom rule is a constraint the tool enforces rather than a constraint recorded in prose. The isolation requirement for a mains-referenced section is otherwise a line in a datasheet or a comment in a schematic, and neither fails a check when the board is re-routed months later. Committing `.kicad_dru` alongside the project makes the constraint travel with the design and re-apply on every DRC run.

The KiCad 9 `creepage` constraint extends this to high-voltage work by distinguishing **surface distance from straight-line clearance** — two measurements that diverge across slots, cutouts and board edges, and that a plain `clearance` constraint cannot separate.

## Exercising a rule

A rule that has never fired has not been shown to work. The check is to construct the violation deliberately: assign the mains nets to an `HV` net class in Board Setup, add the isolation rule, then drag a trace to roughly 0.5 mm from an HV pad and confirm the DRC reports a violation under that rule's name. A rule that produces no marker on a knowingly bad board has a condition that does not match, not a board that is clean.

## Pitfalls

- **A misspelt net class name yields a rule that never fires.** `A.NetClass == 'HV'` compares against a string; if the class in Board Setup is named `HighVoltage`, the condition is false for every pair and the DRC passes silently.
- **A condition naming only `A` leaves `B` unconstrained.** Omitting `B.NetClass != 'HV'` from the isolation rule applies the 1 mm minimum between two HV objects as well, flagging intentional spacing inside the high-voltage section.
- **Double quotes inside a condition terminate it.** The condition string is delimited by double quotes, so literals within it must use single quotes.
- **Custom rules are project-local.** The file is `<project>.kicad_dru`; a rule added to one board does not apply to the next unless the file is copied in, which is why fabricator rulesets are distributed as files.
- **`edge_clearance` is measured to the board outline, not to a keepout.** A rule written against `edge_clearance` says nothing about copper intruding on a user-drawn keepout zone.
- **`creepage` exists only from KiCad 9.** A `.kicad_dru` using it does not evaluate that constraint on earlier versions.
