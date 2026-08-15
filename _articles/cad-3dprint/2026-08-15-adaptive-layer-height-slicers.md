---
title: "Adaptive Layer Height in OrcaSlicer and PrusaSlicer: Fewer Layers Where the Model Is Flat"
date: 2026-08-15
track: cad-3dprint
summary: "A dome printed at a fixed 0.2 mm shows visible stair-steps on its shallow top and wastes time on its vertical sides. Adaptive layer height makes the slicer thin the layers on shallow slopes and thicken them on steep walls automatically. Here is how the tool works in OrcaSlicer 2.4.2 and PrusaSlicer 2.9.6, the settings that bound it, and when to leave it off."
reading_time: 5
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

Stair-stepping is worst exactly where a surface is nearly flat. On a steep wall, a 0.2 mm layer advances the contour a tiny horizontal step; on a 20-degree dome, that same 0.2 mm layer marches the edge outward by more than half a millimetre, and your eye reads the terracing instantly. The fix is not to drop the whole print to 0.1 mm — that doubles print time for walls that never needed it. **Adaptive (variable) layer height** solves the actual problem: thin layers on shallow slopes for finish, thick layers on steep walls for speed, chosen automatically from the surface angle.

This is written against **OrcaSlicer 2.4.2** and **PrusaSlicer 2.9.6** (the June 2026 release). Both descend from the same Slic3r lineage, so the workflow is nearly identical — a one-click adaptive pass, a smoothing pass, and manual paint-over.

## How the slicer decides

The engine walks the model and, for each candidate layer, looks at the steepest surface facet it would cross. Steep (near-vertical) geometry tolerates a tall layer because the horizontal error stays small; shallow geometry forces a short layer to keep the stair-step within a quality target. A single **Quality / Speed** slider biases the whole distribution toward thin-everywhere or thick-everywhere.

In **PrusaSlicer**, select the object, then pick the **Variable layer height** tool from the toolbar. Click **Adaptive** — "calculates the layer profile according to the Quality/Speed setting" — and the model redraws with contour lines showing the new layer bands. **Smooth** runs a Gaussian filter over the profile so heights don't jump abruptly between neighbouring layers (you can press it repeatedly for a gentler gradient), and **Keep min** protects the thinnest green layers from being smoothed away. **Reset** clears the profile back to the fixed height.

**OrcaSlicer** exposes the same idea under its variable-layer-height tool: an **Adaptive** action with a **Quality / Speed** slider, a **Smooth** action with an adjustable Gaussian **radius**, and a vertical bar along the model where you left-click to reduce or right-click to increase the local layer height by hand. The manual paint is the escape hatch for when the automatic pass gets one feature wrong.

## The settings that bound it

Adaptive height is not free to pick any thickness — it is clamped, and understanding the clamps is what separates a clean result from a failed print:

- **Min / max layer height** — the hard limits, set per-extruder in the machine settings (OrcaSlicer: *Printer → Extruder*; PrusaSlicer: *Printer Settings → Extruder*). The adaptive engine only searches inside this band. The practical ceiling is **~75–80% of the nozzle diameter** (0.32 mm for a 0.4 mm nozzle); go higher and layers don't bond reliably.
- **Quality / Speed slider** — biases the distribution. Toward Quality, more of the model drops to the min; toward Speed, more of it rides at the max.
- **Smoothing radius** — how far the Gaussian blends height changes. Too little and you get abrupt transitions that show as banding; too much and you erase the fine layers you wanted.
- **Max volumetric flow** — the sleeper constraint. A thick layer at your normal speed can demand more mm³/s than the hotend can melt; the slicer will slow those layers, eating some of the time you hoped to save. Check the flow warning after slicing.

Here is a sane starting recipe on a 0.4 mm nozzle:

```text
Extruder limits:  min 0.08 mm   max 0.28 mm
Adaptive:         Quality/Speed → slightly toward Quality
Smooth:           apply once (radius ≈ 2–3 layers)
Base layer:       0.20 mm   (unchanged; adaptive varies around it)
Then: paint any critical shallow face down to ~0.10 mm by hand
```

Slice, then open the **preview** and scrub the layer slider: confirm the shallow tops went thin and the vertical walls went thick, and read the time estimate against a fixed-height slice of the same model.

## When NOT to use it

Adaptive height loses on parts where every layer is already doing the same job. It does **nothing useful on a purely vertical model** — a box, a vase, a bracket with no sloped faces — because there are no shallow surfaces to refine; you just add slicing complexity for no gain. It fights **top and bottom shell quality**: adaptive logic keys on outer-surface angle, but a flat top surface still wants a deliberately fine layer for finish, so verify the top layers didn't get thickened. Skip it on **strength-critical functional parts**, where consistent layer bonding and predictable anisotropy matter more than a smoother dome. And it interacts badly with **precise Z features** — parts with a mating height, press-fit, or a specific number of perimeticular layers — because variable heights make an exact Z harder to hit. For those, a fixed height (or a manual per-region modifier) is the honest choice.

**Try next:** Take a model with an obvious shallow dome or angled top, slice it once at a fixed 0.2 mm and note the time and top-surface preview, then run the Adaptive + Smooth pass above and compare — you should see the layer count fall on the walls, the layers thin on the dome, and the estimate move without hurting the visible surface.
