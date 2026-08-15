---
title: "Adaptive Layer Height in OrcaSlicer and PrusaSlicer: Fewer Layers Where the Model Is Flat"
date: 2026-08-15
track: cad-3dprint
summary: "A dome printed at a fixed 0.2 mm shows visible stair-steps on its shallow top and spends time on vertical sides that did not need it. Adaptive layer height has the slicer thin layers on shallow slopes and thicken them on steep walls automatically. This article covers the tool in OrcaSlicer 2.4.2 and PrusaSlicer 2.9.6, the settings that bound it, and the cases where it should be left off."
reading_time: 6
tags: [orcaslicer, prusaslicer, 3d-printing, layer-height, adaptive, slicer]
sources:
  - title: "Variable layer height function — Prusa Knowledge Base"
    url: "https://help.prusa3d.com/article/variable-layer-height-function_1750"
  - title: "OrcaSlicer Wiki — prepare_variable_layer_height"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/wiki/prepare_variable_layer_height"
  - title: "OrcaSlicer v2.4.2 Official Release"
    url: "https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.4.2"
  - title: "PrusaSlicer 2.9.6 Release"
    url: "https://github.com/prusa3d/PrusaSlicer/releases/tag/version_2.9.6"
  - title: "Orca Slicer Adaptive and Variable Layer Height Guide — Obico"
    url: "https://www.obico.io/blog/orca-slicer-adaptive-and-variable-layer-height-guide-smoother-3d-prints/"
---

**Gist.** Stair-stepping on a fused-filament print is a function of surface angle, not of layer height alone: the same layer thickness that is invisible on a vertical wall is glaring on a shallow dome. Adaptive (variable) layer height lets the slicer choose a per-layer thickness from the local surface angle — thin where the slope is shallow, thick where it is steep — instead of applying one height to the whole object. The cost is a Z profile that is no longer uniform: layer bonding varies through the part, exact Z heights become harder to hit, and thick layers can exceed what the hotend can melt at the configured speed.

## Why the angle, and not the height, sets the visible error

For a surface inclined at angle θ above the horizontal plane, a layer of thickness *h* advances the contour horizontally by **h / tan θ**. That quantity is the width of the exposed ledge — the stair-step the eye reads.

On a near-vertical wall, θ approaches 90°, tan θ grows without bound, and the ledge collapses to nothing: a 0.2 mm layer is effectively invisible there. On a **20-degree slope, tan 20° ≈ 0.36, so the same 0.2 mm layer produces a ledge of roughly 0.55 mm** — nearly three times the layer thickness, and plainly visible. Halving the layer height halves the ledge everywhere, which is why the reflexive fix is to slice the entire model at 0.1 mm; that also roughly doubles the layer count on walls whose ledges were already below the threshold of perception. Adaptive layer height exists to decouple the two regimes.

## How the slicer decides

The engine walks the model in Z and, for each candidate layer, considers the slope of the surfaces that layer would cross. Steep geometry tolerates a tall layer because the resulting horizontal error stays small; shallow geometry forces a short layer to keep the error within the quality target. A single **Quality / Speed** control biases the whole distribution toward the thin end or the thick end of the permitted band.

In **PrusaSlicer**, the object is selected and the **Variable layer height** tool chosen from the toolbar. The **Adaptive** action computes a layer profile from the Quality/Speed setting, and the model redraws with contour lines marking the new bands. **Smooth** applies a smoothing filter over the height profile so that adjacent layers do not jump abruptly in thickness; it can be applied repeatedly for a gentler gradient. **Keep min** protects the thinnest layers from being smoothed away — without it, the smoothing pass tends to raise exactly the fine layers that motivated the adaptive pass. **Reset** discards the profile and returns the object to its fixed height.

**OrcaSlicer 2.4.2** exposes the same construction under its variable-layer-height tool: an **Adaptive** action with a **Quality / Speed** slider, a **Smooth** action with an adjustable **radius**, and a vertical bar alongside the model where mouse clicks raise or lower the local layer height. **Manual painting is the escape hatch**: the automatic pass keys on geometry alone and has no notion of which face the part will be judged by, so a single cosmetic feature can be corrected by hand without disturbing the rest of the profile.

Both programs descend from the same Slic3r lineage, which is why the workflow — adaptive pass, smoothing pass, manual override — is near-identical across them. **PrusaSlicer 2.9.6** is the release referenced here.

## The clamps

The adaptive search is not free to choose any thickness. It operates inside a band, and the band is what separates a clean result from a failed print.

- **Minimum and maximum layer height** — the hard limits, configured per extruder in the machine settings (OrcaSlicer: *Printer → Extruder*; PrusaSlicer: *Printer Settings → Extruder*). The adaptive engine searches only within this band, so a profile that looks insufficiently varied is often a band that is too narrow rather than a slider set wrongly. The practical ceiling is around **75–80% of the nozzle diameter — about 0.32 mm on a 0.4 mm nozzle**; above that, layers stop bonding reliably.
- **Quality / Speed** — the bias within the band. Toward Quality, more of the model sits at the minimum; toward Speed, more of it rides at the maximum.
- **Smoothing radius** — the extent over which the smoothing filter blends height changes. Too small a radius leaves abrupt transitions that print as banding; too large a radius averages the thin layers back toward their thicker neighbours and erases the refinement.
- **Maximum volumetric flow** — the constraint that is easiest to miss. A thick layer at the normal print speed demands more cubic millimetres per second than a thin one; where that exceeds what the hotend can melt, the slicer reduces speed on those layers, returning part of the time the thick layers were meant to save. The flow warning after slicing is the place this shows up.

A starting configuration on a 0.4 mm nozzle:

```text
Extruder limits:  min 0.08 mm   max 0.28 mm
Adaptive:         Quality/Speed → slightly toward Quality
Smooth:           apply once (radius ~ 2-3 layers)
Base layer:       0.20 mm   (unchanged; adaptive varies around it)
Then: paint any critical shallow face down to ~0.10 mm by hand
```

Verification is done in the **preview**, by scrubbing the layer slider: the shallow tops should have gone thin and the vertical walls thick. The time estimate is only meaningful against a fixed-height slice of the same model, sliced for comparison.

## Where the technique does not apply

Adaptive height has nothing to work with on a **purely vertical model** — a box, a vase, a bracket with no sloped faces — because there are no shallow surfaces whose error can be reduced; the profile converges on the fixed height and the added complexity buys nothing.

It also interacts poorly with **top and bottom shell quality**. The adaptive criterion is outer-surface angle, and a flat top surface is horizontal rather than shallow, so the top layers can end up thickened even though finish there depends on a fine layer. Those layers should be inspected explicitly after the adaptive pass.

On **strength-critical functional parts**, consistent layer bonding and predictable anisotropy are the properties being relied on; a profile that varies thickness through the part varies both. And on parts with **precise Z features** — a mating height, a press fit, a specific layer count in a region — a variable profile makes an exact Z harder to land on, because the height of a given layer boundary is now the sum of a non-uniform sequence. A fixed height, or a manual per-region modifier, keeps that arithmetic exact.

## Pitfalls

- **The adaptive pass appears to do nothing.** The minimum and maximum layer heights in the extruder settings are close together, so the search band admits almost no variation; the slider cannot widen a band it does not control.
- **Fine layers disappear after smoothing.** The smoothing filter averages each layer against its neighbours, and without **Keep min** the thinnest layers are pulled up toward the thicker ones surrounding them.
- **Visible banding on a smooth curve.** The smoothing radius is too small, leaving a step change in thickness between adjacent bands that reads as a horizontal line on the surface.
- **The predicted time saving does not materialise.** The thick layers exceed the maximum volumetric flow, and the slicer has reduced feed rate on those layers to stay within what the hotend can melt.
- **Thick layers delaminate.** The maximum layer height is set above roughly 75–80% of the nozzle diameter, so the extruded bead does not press into the layer below with enough contact to bond.
- **A part no longer fits its mate.** The mating feature's Z is now the sum of layers of differing thickness rather than a multiple of one height, so the boundary lands off the intended plane.
- **A flat top surface came out coarse.** The adaptive criterion reacts to surface angle; a horizontal top offers no slope to trigger refinement, and the layers there were thickened by the Speed bias.
