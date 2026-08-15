---
title: "Panelizing KiCad Boards with KiKit: A 2x2 Panel from One Command"
date: 2026-08-15
track: cad-3dprint
summary: "Small boards below fab minimums, per-board assembly cost, and pick-and-place rails all point the same way: panelize. KiKit turns one .kicad_pcb into a framed multi-board panel — mousebites or v-cuts, tooling holes, fiducials — from a single CLI command or a reusable JSON preset."
reading_time: 5
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

A 20 × 15 mm sensor breakout is a perfectly good board and a terrible fabrication order. JLCPCB's minimum FR4 board size is 3 × 3 mm, but assembly lines want something they can actually grip: rails for the conveyor, tooling holes for registration, fiducials for the pick-and-place camera. And whether you order five boards or a 2×2 panel of them, you often pay per *delivered unit* — so panelizing turns one prototype run into four boards for nearly the same money. **KiKit** automates all of it from the command line against your existing `.kicad_pcb`, no manual copy-paste-and-pray in the PCB editor. Current release is **v1.8.0** (April 2026), which supports KiCad 9 and the new KiCad 10 and drops everything older than 9; KiCad 9 support originally landed in v1.7.0. Install with `pip install kikit` (the docs cover getting it into KiCad's bundled Python on each platform).

## Mousebites or v-cuts?

The two separation methods have different fab-imposed rules, so pick before you panelize.

**V-cuts** are straight scored grooves cut across the *entire* panel — no partial cuts, no curves. JLCPCB requires v-cut panels to be at least **70 × 70 mm**, board thickness ≥ 0.6 mm, and leaves about **1/3 of the thickness** as the connecting web. Keep copper ≥ 0.4 mm from the cut centerline and components 1–2 mm away. In exchange you get near-zero wasted material and clean straight edges. Use them for rectangular boards big enough to meet the minimum.

**Mousebites** (stamp holes) are routed tabs perforated with small drills — they work for any outline, curved edges included, and JLCPCB imposes *no minimum panel size* for them. The cost is a rougher edge you may need to file, and material lost to the routed gap. For odd-shaped hobby boards and anything tiny, mousebites are the default.

Either way the panel needs **rails**: PCBWay wants breakaway rails at least 3 mm wide (5 mm is the comfortable default), fiducials of ~1 mm bare copper placed ≥ 5 mm from the panel edge in an asymmetric L-pattern, and ~2 mm non-plated tooling holes.

## One command, one panel

Here is a complete 2×2 mousebite panel with top/bottom rails, tooling holes, and fiducials — straight from KiKit's documented example set:

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

Reading it section by section: **layout** places copies in a grid with 2 mm routed gaps; **tabs** puts two 3 mm-wide bridges on each vertical edge; **cuts** perforates those tabs with 0.5 mm drills at 1 mm pitch, pulled 0.2 mm *inside* the board outline so the broken stubs don't protrude past the edge; **framing** adds 5 mm rails top and bottom, 3 mm from the boards; **tooling** and **fiducials** decorate the rails; and **post** `millradius: 1mm` rounds every internal corner to what a real routing bit can actually cut, so your preview matches what ships. All lengths must carry units (`mm`, `mil`, `inch`). Open `panel.kicad_pcb` in KiCad, run DRC, and generate fabrication outputs from it exactly as you would for a single board — the Gerber and IPC-2581 flow from the earlier fabrication-outputs article applies unchanged.

For a v-cut version, swap two sections: `--cuts 'vcuts'` and `--tabs 'full'` (v-cuts must span the panel, so the boards connect across their full edge), and use `--framing 'frame; width: 5mm; space: 3mm; cuts: both'` so the frame separates from the boards too.

## Presets: stop retyping fab rules

Every CLI section can live in a JSON preset. KiKit merges its built-in defaults, then each `-p` file in order, then CLI flags — so you keep one file per fab and per separation style:

```json
{
    "layout": { "type": "grid", "rows": 2, "cols": 2, "space": "2mm" },
    "tabs": { "type": "fixed", "width": "3mm", "vcount": 2 },
    "cuts": { "type": "mousebites", "drill": "0.5mm", "spacing": "1mm", "offset": "0.2mm" },
    "framing": { "type": "railstb", "width": "5mm", "space": "3mm" },
    "post": { "millradius": "1mm" }
}
```

Then `kikit panelize -p jlc-mousebites.json breakout.kicad_pcb panel.kicad_pcb`. KiKit also ships built-in presets referenced with a colon prefix — `kikit panelize -p :jlcTooling ...` applies JLCPCB's assembly tooling-hole convention without you looking up the numbers.

## Common failures

**Tabs through pads.** KiKit places tabs by geometry, not by what's on the board — a tab landing under an edge connector or castellated pad means the router chews your copper. Fix it by moving tabs explicitly: place `kikit:Tab` annotation footprints on the source board where tabs are safe, then use `--tabs annotation`. Note KiKit builds *half-bridges* from each annotation, so the opposite side needs matching substrate — a facing tab, a backbone, or a tightframe.

**Mousebite breakout roughness.** Drill too large or spacing too wide and separation tears fiberglass past the outline. Stay near the 0.5 mm drill / 1 mm spacing defaults and keep the 0.2 mm inward offset; the stubs then break *inside* the theoretical edge and a quick file pass finishes the job.

**Illegal v-cuts.** A v-cut that stops mid-panel, runs at an angle, or sits on a 0.4 mm-thick board will bounce at review. KiKit's v-cut validation (added in 1.7.0) catches most of these before the fab does.

**Sharp internal corners.** Without `millradius`, KiKit draws internal corners a 2 mm router bit cannot produce. Always set it; 1 mm matches common fab tooling.

**Try next:** panelize your smallest real board into a 2×2 with the command above, diff the panel's DRC report against the single board's, and order it — then check whether the mousebite stubs land inside or outside the edge you designed.
