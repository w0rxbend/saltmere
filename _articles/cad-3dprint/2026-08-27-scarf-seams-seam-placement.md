---
title: "Seam Placement and Scarf Joints: How OrcaSlicer Hides the Z-Seam"
date: 2026-08-27
track: cad-3dprint
summary: "Every closed perimeter loop starts and stops somewhere, and that point prints as a visible Z-seam. This article walks OrcaSlicer's seam-position strategies — aligned, back, nearest, random, and paint-on — and the scarf-joint seam introduced in OrcaSlicer 2.0, which ramps Z height and extrusion flow over an overlapping start/end segment so the seam is diffused over tens of millimetres instead of concentrated at a point. The costs are print time, over-extrusion artifacts on some materials, and dimensional effects on sealing surfaces."
reading_time: 6
tags: [orcaslicer, z-seam, scarf-joint, fdm, slicing, surface-quality]
sources:
  - title: "OrcaSlicer Wiki — Seam settings"
    url: "https://www.orcaslicer.com/wiki/print_settings/quality/quality_settings_seam"
  - title: "OrcaSlicer v2.0.0 release notes"
    url: "https://github.com/SoftFever/OrcaSlicer/releases/tag/v2.0.0"
  - title: "All3DP — OrcaSlicer 2.0 Released, Improves Seams with 'Scarf Joints'"
    url: "https://all3dp.com/4/orcaslicer-2-0-releases-improves-seams-with-scarf-joints/"
---

**Gist.** A fused deposition modelling (FDM) perimeter is a closed loop, so the extruder must start and stop at the same point, and the pressure transient at that point prints as a visible vertical line — the Z-seam. OrcaSlicer offers two orthogonal responses: *place* the seam where it is least visible (aligned, back, nearest, random, or painted by hand), or *diffuse* it with a scarf joint, which overlaps the loop's start and end while ramping Z height and extrusion flow so no single point carries the full start/stop transient. The scarf trades that cosmetic gain for longer print time, potential over-extrusion artifacts on some materials, and a slight thickening of the wall along the overlap — which matters on sealing and mating surfaces.

## Why the seam exists at all

An extruder is a pressure vessel with lag. When a perimeter loop begins, melt pressure in the nozzle must rise from the travel-move level to the steady-state printing level; when the loop ends, pressure must fall again. Both transients happen at the same XY location, so each layer deposits either **a small gap (under-extrusion during the ramp-up) or a blob (residual pressure at the stop)** at that point. Stacked over hundreds of layers, these point defects form the familiar vertical scar.

The seam cannot be eliminated by tuning alone. Pressure advance, retraction, and a small **seam gap** (OrcaSlicer's wiki suggests **0–15 % of nozzle diameter** on a well-tuned machine) shrink the defect, but a closed loop still has exactly one discontinuity per layer. Everything after that is a placement or diffusion strategy.

## The placement strategies

OrcaSlicer's `seam position` setting decides, per loop, where the discontinuity lands. The strategies differ in what they optimise:

- **Aligned** attempts to place the seam on a hidden internal facet of the model — a concave corner or recessed feature — and to keep successive layers' seams vertically coherent. Concave corners are good hosts because the blob sits inside the corner rather than proud of the surface.
- **Aligned back** combines the aligned heuristic with a preference for the side facing away from the front, for models with a display orientation.
- **Back** places the seam at the loop's **minimum-Y point**, producing one straight line on the rear of the part. Predictable, and acceptable for parts viewed from one side.
- **Nearest** starts each loop wherever the nozzle already is. This minimises travel moves — and therefore stringing and time — but scatters seams over the whole surface.
- **Random** deliberately scatters the seam. The OrcaSlicer wiki frames this as a *strength* choice: a vertically aligned seam is a vertically aligned weak line, and randomising it distributes the weak points instead of stacking them. The cosmetic result is a surface peppered with small defects rather than one line.
- **Paint-on seam** overrides the cost function entirely: the user paints regions in the preparation view, and the slicer constrains seam placement to (or away from) the painted area. This is the only strategy with knowledge the heuristics lack — which face the user cares about.

The unstated model behind aligned/back is a **cost function over candidate seam vertices**: visibility (convexity of the local corner, facing direction) and continuity with the layer below are scored, and the cheapest vertex wins. The practical consequence is that **geometry drives seam quality**: a cylinder has no concave corner to hide in, so aligned degenerates to an arbitrary straight scar. Cylinders are precisely where the scarf joint earns its place.

## The scarf joint: diffusing instead of hiding

A scarf joint — the term is borrowed from woodworking, where two boards are joined on a long taper rather than a butt end — replaces the point seam with an **overlapping ramp**. OrcaSlicer shipped it in version 2.0.0. The mechanism, per the OrcaSlicer wiki:

1. The loop's start is lowered: printing begins **below full layer height** (the *scarf height* setting, in mm or as a percentage of layer height) and ramps up to full height over the *scarf length* (the wiki's example default is **20 mm**), discretised into at least *scarf steps* segments (default **10**).
2. The loop's end retraces the same segment on top of the ramp, ramping **down** as the start ramped up, so the two wedges sum to one full-height extrusion.
3. Flow is adjusted in concert with the ramp so the deposited volume matches the wedge cross-section; a separate *scarf joint flow ratio* scales this (recommended **100 %**, and moved to developer-mode only in v2.0.0 because, per the release notes, "its utility remains unclear based on testing results").

The start/stop pressure transients still happen — but each now occurs on a **partial-height, partial-flow wedge whose error is spread over the scarf length**, not stacked at one XY point. On a smooth curved wall the seam line effectively disappears.

Two guards keep the scarf from doing harm. **Conditional scarf** applies the joint only on smooth, curved perimeters and falls back to a conventional seam at sharp corners, where a ramp would round the corner off. An **overhang angle threshold**, added in v2.0.0, disables the scarf on steep overhangs — the release notes state plainly that the scarf "doesn't play well with steep overhangs", since a half-height ramp printed over air has nothing to bond to. The wiki additionally recommends keeping scarf speed **under 100 mm/s**; above that, the slicer clamps to the slower of the scarf and wall speeds.

## What the scarf costs

- **Time.** Every outer loop gains an overlapping retraced segment; the *scarf around entire wall* option (wrapping the ramp around the full perimeter) multiplies this and is disabled by default for that reason. On a print that is mostly perimeters, the overhead is proportional to scarf length times loop count.
- **Over-extrusion artifacts.** The overlap region receives material from two passes whose flows must sum exactly to one. Any flow calibration error accumulates there, and materials that are unforgiving of over-extrusion — glossy filaments that telegraph surface height differences — can show a **slightly raised or shiny band the length of the scarf** where a point seam would have shown a dot. No published measurement quantifies this per material; the observable claim is that the artifact scales with flow calibration error and scarf length.
- **Dimensional effect on sealing surfaces.** The scarf region is nominally net-zero in volume, but the two wedges are deposited at different times and pressures, so the wall along the overlap is the least dimensionally controlled part of the loop. On a bore that must seal against an O-ring, or a shaft that must slide in a printed bushing, a 20 mm band of slightly proud wall is a functional defect, not a cosmetic one. For such surfaces the conventional choice — an aligned seam rotated away from the sealing contact, or a painted seam — is safer than a scarf.

The decision rule that falls out: **scarf for visible curved cosmetic surfaces; aligned or painted point seams for dimensional and sealing surfaces; random where interlayer strength matters more than appearance**.

## Pitfalls

- A scarf on a steep overhang detaches or droops because the half-height ramp start has reduced contact with the layer below; the v2.0.0 overhang threshold exists to suppress exactly this.
- A scarf across a sharp corner rounds it, because the ramp cannot reproduce the corner's flow discontinuity; conditional scarf must remain enabled for parts with mixed curved and angular walls.
- Reducing scarf joint flow ratio below 100 % to fight the shiny band trades it for under-extrusion pinholes along the ramp; the setting is developer-mode only in v2.0.0 for this reason.
- `Nearest` seam position on a batch of identical parts produces different seam patterns per part, because each loop's start depends on the previous toolpath, not the geometry.
- A random seam on a transparent or translucent filament reads as worse, not better: scattered defects refract light across the whole wall instead of confining it to one line.
- Painting a seam onto a surface with no valid vertices in the painted band (a fully convex fine-featured region) silently falls back to the heuristic, and the seam lands where the paint was meant to forbid it.
