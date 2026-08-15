---
title: "Panelizing KiCad Boards with KiKit: A 2x2 Panel from One Command"
date: 2026-08-15
track: cad-3dprint
summary: "Small boards fall below assembly-line handling minimums and lack rails, tooling holes and fiducials. KiKit transforms one .kicad_pcb into a framed multi-board panel — mousebites or v-cuts — from a single CLI invocation or a reusable JSON preset, at the cost of fab-specific geometry rules that must be encoded correctly."
reading_time: 6
tags: [kicad, kikit, panelization, mousebites, v-cuts, pcb, fabrication]
sources:
  - title: "KiKit — Automation tools for KiCAD (GitHub)"
    url: "https://github.com/yaqwsx/KiKit"
  - title: "KiKit documentation: Panelization CLI"
    url: "https://yaqwsx.github.io/KiKit/latest/panelization/cli/"
  - title: "KiKit documentation: Panelization examples"
    url: "https://yaqwsx.github.io/KiKit/latest/panelization/examples/"
  - title: "JLCPCB: V-Cut panelization standards"
    url: "https://jlcpcb.com/blog/v-cut-panelization-standards"
  - title: "PCBWay: PCB panelization, breakaway rails, fiducial marks, tooling holes"
    url: "https://www.pcbway.com/helpcenter/design_instruction/PCB_Panelization__Breakaway_Rails__Fiducial_Marks__Tooling_Holes.html"
---

**Gist.** A 20 × 15 mm breakout is well within what a fabricator will etch, but it offers an assembly line nothing to grip, register against, or align a placement camera to. Panelization solves this by replicating the board into a grid, bridging the copies with breakable connections, and surrounding the array with rails that carry tooling holes and fiducials; KiKit performs that transformation on an existing `.kicad_pcb` from one command line. The cost is that panel geometry is governed by fab-specific rules — minimum panel size, cut-to-copper clearance, router-bit radius — which must be encoded in the invocation, because the tool applies the geometry requested rather than the geometry a given fabricator accepts.

## What a panel adds to a board

Three features exist for the machines, not the circuit. **Breakaway rails** give the conveyor and the board-handling clamps a region that is not part of any delivered board. **Tooling holes**, non-plated and drilled in the rails, register the panel mechanically in fixtures. **Fiducials**, a disc of bare copper inside a solder-mask opening, give the pick-and-place camera optical reference points; the usual arrangement places three of them on the rails in an asymmetric pattern rather than a symmetric one, so that a panel loaded the wrong way round does not present the same fiducial geometry. Rails are commonly 5 mm wide.

KiKit tracks KiCad's own release line, and a given KiKit release states which KiCad major versions it supports; the pairing must be checked against the release notes rather than assumed. Installation is `pip install kikit`, with the documentation covering placement into KiCad's bundled Python interpreter per platform.

## The separation mechanism: two incompatible geometries

The choice of how copies detach determines what outlines and panel sizes are legal, so it precedes panelization rather than following it.

**V-cuts** are scored grooves cut straight across the *entire* panel. The constraint is structural: the blade traverses the full width, so a cut cannot stop mid-panel and cannot follow a curve. JLCPCB's documented requirements are a panel of at least **70 × 70 mm**, board thickness **≥ 0.6 mm**, and a remaining web of about **1/3 of the board thickness** left uncut. Copper must stay **≥ 0.4 mm** from the cut centerline with components kept back further still, because the groove and the subsequent snap both disturb material adjacent to the line. The compensating property is that adjacent boards share an edge, so material loss between copies approaches zero and the resulting edge is straight.

**Mousebites** are routed slots interrupted by tabs, each tab perforated by a row of small drills. Because the separation is produced by routing rather than by a full-width blade pass, the outline may be arbitrary, curves included, and the minimum panel size that applies to v-scoring does not apply. The costs are the material removed by the router slot and a fractured edge at each perforation, which typically requires filing. For irregular outlines and boards below the v-cut size floor, mousebites are the only admissible option.

## One command, one panel

The following produces a 2×2 mousebite panel with top and bottom rails, tooling holes and fiducials, drawn from KiKit's documented example set:

```bash
kikit panelize \
    --layout 'grid; rows: 2; cols: 2; space: 2mm' \
    --tabs 'fixed; width: 3mm; vcount: 2' \
    --cuts 'mousebites; drill: 0.5mm; spacing: 1mm; offset: 0.2mm; prolong: 0.5mm' \
    --framing 'railstb; width: 5mm; space: 3mm' \
    --tooling '3hole; hoffset: 2.5mm; voffset: 2.5mm; size: 1.5mm' \
    --fiducials '3fid; hoffset: 5mm; voffset: 2.5mm; coppersize: 2mm; opening: 1mm' \
    --post 'millradius: 1mm' \
    breakout.kicad_pcb panel.kicad_pcb
```

Each section transforms the intermediate panel in turn. **`layout`** replicates the source board into a grid with 2 mm of routed gap between copies. **`tabs`** reintroduces substrate across that gap: two bridges, each 3 mm wide, on every vertical edge. **`cuts`** perforates those bridges with 0.5 mm drills at 1 mm pitch; **`offset: 0.2mm` pulls the perforation line 0.2 mm inside the board outline**, so the stubs left after snapping terminate inside the nominal edge instead of protruding past it. **`framing: railstb`** adds 5 mm rails above and below the array, separated from the boards by 3 mm. **`tooling`** and **`fiducials`** place their features on those rails. **`post` `millradius: 1mm`** replaces every internal corner with an arc of 1 mm radius, matching what a rotating router bit can physically produce — a square internal corner is not a shape a cylindrical cutter can cut, so without this step the panel drawing and the manufactured panel differ. **All lengths must carry an explicit unit** (`mm`, `mil`, `inch`).

The output `panel.kicad_pcb` is an ordinary KiCad board file: it opens in the editor, runs through design rule check (DRC), and produces Gerber and IPC-2581 fabrication outputs by the same procedure as a single board.

Converting the same panel to v-cuts requires two changes that follow from the full-width constraint: `--cuts 'vcuts'` and `--tabs 'full'`, the latter because boards must remain connected across their entire shared edge for a straight cut to be meaningful. The frame section becomes `--framing 'frame; width: 5mm; space: 3mm; cuts: both'` so the frame itself separates from the array.

## Presets as encoded fab rules

Every CLI section has a JSON equivalent. KiKit resolves configuration by merging **built-in defaults first, then each `-p` file in the order given, then command-line flags**, so a per-fabricator file and a per-separation-style file compose without either being rewritten.

```json
{
    "layout": { "type": "grid", "rows": 2, "cols": 2, "space": "2mm" },
    "tabs": { "type": "fixed", "width": "3mm", "vcount": 2 },
    "cuts": { "type": "mousebites", "drill": "0.5mm", "spacing": "1mm", "offset": "0.2mm" },
    "framing": { "type": "railstb", "width": "5mm", "space": "3mm" },
    "post": { "millradius": "1mm" }
}
```

The file is applied with `kikit panelize -p jlc-mousebites.json breakout.kicad_pcb panel.kicad_pcb`. KiKit also ships built-in presets, referenced with a colon prefix: `-p :jlcTooling` applies JLCPCB's assembly tooling-hole convention without the numbers being restated locally.

## Pitfalls

**Tabs landing on pads.** Tab placement in `fixed` mode is derived from edge geometry alone, with no knowledge of copper, so a tab can fall across an edge connector or castellated pad and the router removes that copper. The remedy is explicit placement: `kikit:Tab` annotation footprints on the source board plus `--tabs annotation`. Each annotation generates a **half-bridge**, so the facing side needs matching substrate — an opposing tab, a backbone, or a tightframe — or the bridge terminates in empty space.

**Under-perforated mousebites.** Widening `spacing` without widening `drill` leaves more unperforated fiberglass between holes, and separation then tears material outside the intended outline instead of fracturing along the hole line.

**Zero or omitted cut offset.** Without the 0.2 mm inward offset the fracture occurs on the nominal outline, and the residual stubs protrude beyond the designed edge, which can defeat an enclosure fit that assumed the outline dimension.

**Illegal v-cuts.** A v-cut that stops mid-panel, runs at an angle, or is specified on a board below the 0.6 mm thickness minimum violates the fab rule and is rejected at review, after the design has already been submitted.

**Omitted `millradius`.** Internal corners are drawn sharp, the fab's router cannot reproduce them, and the delivered outline diverges from the panel drawing at every internal corner.
