---
title: "Tree Supports in OrcaSlicer: Organic Supports That Peel Off Clean"
date: 2026-08-14
track: cad-3dprint
summary: "Tree supports touch the model in fewer places and use a third less material than grid — but they only peel off clean if the branch geometry and the top Z gap are dialed in. Here are the OrcaSlicer settings that matter and a recipe for PLA."
reading_time: 5
tags: [orcaslicer, 3d-printing, supports, tree-supports, organic, slicer]
sources:
  - title: "OrcaSlicer Wiki: Tree Support (support_settings_tree)"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/support_settings_tree"
  - title: "OrcaSlicer Wiki: Support (support_settings_support)"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/support_settings_support"
  - title: "OrcaSlicer v2.3.0 release"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.3.0"
  - title: "Tree Supports, Interface Layers, and Clean Removal — UAVMODEL (2026)"
    url: "https://blog.uavmodel.com/3d-printer-support-settings-tree-supports-interface-layers-and-clean-removal-techniques-2026-guide/"
---

Grid supports build a dense wall under every overhang and touch the model along a broad flat contact — sturdy, wasteful, and often welded to the part. Tree supports instead grow thin branches up from the build plate (or the model) and reach the overhang with small tips, contacting only where the print actually needs held up. On the OrcaSlicer 2.3 series (2.3.1 is current stable, February 2026), the polished variant of this is the **Organic** style, and when its geometry is set right the whole tree lifts off in one piece. This is written against 2.3.

## Tree vs. normal, and when to reach for each

In OrcaSlicer, **Support type** offers `normal(auto)`, `tree(auto)`, and the manual variants. Choosing a tree type unlocks the **Style** dropdown, whose tree options are **Tree Slim**, **Tree Strong**, **Tree Hybrid**, and **Organic** — the last described in the wiki as merging slim and organic branches more aggressively to save material. (Grid and Snug are the styles for `normal` supports.)

Tree/organic supports win for **organic shapes, miniatures, figurines, and complex geometry with sparse or awkward overhangs**, using roughly 30–50% less material than grid because they skip the solid wall. Grid still wins for **large flat overhangs close to the bed** and blocky mechanical parts, where a broad flat support surface gives a cleaner down-face than a scatter of branch tips.

## The settings that decide clean removal

Two things govern whether a tree peels off or fuses on: the **branch geometry** (how the tree is built) and the **top Z distance** (the air gap between the last support layer and the part).

Branch geometry, all under Advanced mode:

- **Branch angle** (`tree_support_branch_angle`) — the maximum overhang angle a branch may lean at. Steeper leans save material but grow fragile; 40–50° is the practical band.
- **Branch diameter** (`tree_support_branch_diameter`) — initial diameter of the trunk/nodes. Bigger is more stable, harder to snap off; 2–3 mm is typical.
- **Tip diameter** (`tree_support_tip_diameter`) — the diameter where a branch meets the model. Small tips leave small scars; around 2.5 mm balances scar size against a stable landing.
- **Branch diameter angle** (`tree_support_branch_diameter_angle`) — taper; `0` gives uniform-thickness branches.

Contact and interface, mostly under the general Support page:

- **Top Z distance** — the air gap that makes support *removable* instead of welded. One layer height (0.2 mm for a 0.2 mm layer) is the standard starting point.
- **Top interface layers** — dense layers just under the part that improve the down-face finish. 2–3 layers is the sweet spot; more finish, slightly harder removal.
- **Interface density / spacing** — 60–80% density (Concentric pattern reads well under tree tips).
- **Support on build plate only** ("On build plate only") — forbids branches landing on the model surface, so nothing scars a visible face. Ideal for figurines; leave it off when internal overhangs genuinely need support.

## A PLA recipe to start from

Set the process to `tree(auto)`, Style **Organic**, then:

```text
Support type ............. tree(auto)
Style .................... Organic
Top Z distance ........... 0.20 mm   (= 1 layer at 0.2 mm)
Bottom Z distance ........ 0.20 mm
Top interface layers ..... 2
Interface density ........ 60 %
Interface pattern ........ Concentric
Branch angle ............. 40°
Branch diameter .......... 2.0 mm
Tip diameter ............. 2.5 mm
Branch diameter angle .... 5°
On build plate only ...... on   (figurines) / off (internal overhangs)
```

Material shifts the gap: PETG wants a larger **Top Z distance** (~0.30 mm) and one more interface layer because it bonds aggressively; TPU wants 0.40 mm and lower interface density. ABS/ASA behaves like PLA at ~0.20 mm.

Slice, then **preview** and step through the support layers before committing filament. Look for two failure signs: branches sprouting mid-air with no path to the plate (raise branch angle or turn off "on build plate only"), and tips landing on a show face (turn "on build plate only" on, or nudge the model's orientation). Print one test, then tune a single variable — if removal fights you, add 0.05 mm to Top Z distance; if the down-face is rough, add an interface layer or raise interface density.

**Try next:** take a model you last printed on grid supports, reslice it with the Organic recipe above, and compare the two — support weight in the slicer's estimate, and how the tree comes off the part in your hand.
