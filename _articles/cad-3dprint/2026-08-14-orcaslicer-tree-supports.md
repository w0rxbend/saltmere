---
title: "Tree Supports in OrcaSlicer: Branch Geometry and Clean Removal"
date: 2026-08-14
track: cad-3dprint
summary: "Tree supports contact the model at fewer points and use less material than grid, but they separate cleanly only when branch geometry and the top Z distance are set correctly. The OrcaSlicer settings that govern removal, and a PLA starting recipe."
reading_time: 6
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

**Gist.** Grid supports hold an overhang up by building a dense wall beneath it and meeting the part across a broad flat contact, which consumes filament and frequently fuses to the model. Tree supports replace the wall with thin branches that rise from the build plate (or from the model) and terminate in small tips placed only where load must be carried, which reduces the volume of support material for the same overhang set. The cost is that stability now depends on branch geometry rather than on bulk: the branch angle, diameter and tip diameter must be traded against each other, and the top Z distance must be large enough to prevent welding yet small enough to leave a usable down-face.

This article is written against the OrcaSlicer 2.3 series.

## Support type and style

OrcaSlicer exposes **Support type** with the values `normal(auto)`, `tree(auto)`, and the corresponding manual variants. Selecting a tree type enables the **Style** dropdown, whose tree entries are **Tree Slim**, **Tree Strong**, **Tree Hybrid**, and **Organic**. **Grid** and **Snug** are the styles available to `normal` supports; they do not apply to tree types.

The selection is governed by the shape of the overhang set rather than by a general preference. Tree and Organic supports suit **organic shapes, miniatures, figurines, and complex geometry with sparse or awkward overhangs**, where a scatter of branch tips reaches every unsupported region without enclosing the part in a wall. Grid retains the advantage for **large flat overhangs close to the bed** and for blocky mechanical parts: a broad flat support surface presents a continuous plane to the first overhang layer, whereas isolated branch tips leave the extrusion to bridge between landing points, which shows as a rougher down-face.

## The two variables that decide removal

Whether a tree peels away in one piece or fuses to the part is decided by two independent groups of settings: the **branch geometry**, which determines how much force the tree can transmit and how much cross-section must be broken to detach it, and the **top Z distance**, which determines whether support and part are mechanically joined at all.

### Branch geometry

These parameters live under Advanced mode.

- **Branch angle** (`tree_support_branch_angle`) — the maximum angle from vertical at which a branch may lean. Steeper leans let one trunk serve overhangs further from its base, so fewer trunks and less material are required, but a leaning branch is more fragile. **40-50° is the practical band.**
- **Branch diameter** (`tree_support_branch_diameter`) — the initial diameter of the trunk and nodes. Larger diameters are more stable under the load of the overhang above and harder to snap off afterwards. **2-3 mm is typical.**
- **Tip diameter** (`tree_support_tip_diameter`) — the diameter where a branch meets the model, and therefore the size of the scar left behind. Small tips leave small scars but present a smaller landing area for the first overhang extrusion. The tip is narrower than the branch diameter, and the OrcaSlicer default is the reasonable starting point; the two effects trade directly against each other, and no published measurement fixes an optimum.
- **Branch diameter angle** (`tree_support_branch_diameter_angle`) — the taper applied along a branch. A value of **`0` produces uniform-thickness branches**; larger values thicken the branch toward its base.

### Contact and interface

These sit mostly on the general Support page.

- **Top Z distance** — the air gap between the last support layer and the part surface. This gap is what makes the support *removable* rather than welded: without it, the support interface and the model's down-face are deposited in contact and bond as one solid. **One layer height (0.20 mm at a 0.20 mm layer) is the standard starting point.**
- **Top interface layers** — dense layers immediately beneath the part that improve the down-face finish by giving the overhang a near-continuous surface to print onto. **2-3 layers is the usual band**; more layers improve the finish and make removal marginally harder.
- **Interface density / spacing** — **60-80% density**, with the Concentric pattern reading well beneath tree tips.
- **Support on build plate only** — forbids branches from landing on the model surface, so no branch tip scars a visible face. Appropriate for figurines; it must be off where internal overhangs require support, because those overhangs have no path to the plate.

## A PLA starting recipe

Set the process to `tree(auto)` and Style **Organic**, then:

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
Tip diameter ............. default
Branch diameter angle .... 5°
On build plate only ...... on   (figurines) / off (internal overhangs)
```

Material changes the required gap because it changes how strongly the support interface bonds to the down-face. PETG bonds aggressively and wants a larger **Top Z distance** — around 0.30 mm — plus one additional interface layer. TPU wants a larger gap again, on the order of 0.40 mm, and a lower interface density. ABS and ASA behave much like PLA. These are the starting points that circulate as practice rather than measured optima; no controlled comparison separates them.

## Verification before printing

After slicing, the support layers should be stepped through in preview. Two failure modes are visible there and not on the plate:

1. **Branches that sprout in mid-air with no path down to the build plate.** The tree cannot reach the required landing point under the current constraints. Raising the branch angle, or turning off "on build plate only", restores a path.
2. **Tips landing on a show face.** Enabling "on build plate only", or changing the model's orientation, moves the landing points off the visible surface.

Tuning proceeds one variable at a time from a single test print. If removal requires excessive force, add 0.05 mm to Top Z distance. If the down-face is rough, add an interface layer or raise interface density.

## Pitfalls

- **Top Z distance set to zero welds the support to the part.** With no air gap the interface layer and the model's down-face are extruded in contact and fuse; removal then tears material out of the part rather than separating at a boundary.
- **Interface density raised to close the gaps in the down-face makes the support harder to remove.** Density and removability move in opposite directions, because both are governed by how much interface material is bonded under the overhang.
- **Increasing branch angle beyond the 40-50° band makes collapse mid-print more likely.** A branch leaning further from vertical carries the load above it at a larger moment arm and fails before it reaches its landing point.
- **Leaving "on build plate only" enabled on a model with internal overhangs leaves those overhangs unsupported.** The setting forbids branches from landing on the model, and an internal overhang has no route to the plate, so no support is generated there.
- **Reusing the PLA gap of 0.20 mm for PETG risks fused supports.** PETG bonds more aggressively, and the air gap that separates cleanly in PLA can be bridged.
- **Small tip diameters reduce scarring but shrink the landing area.** Below a workable tip size the first overhang layer bridges between distant points and the down-face degrades even though the interface settings are unchanged.
