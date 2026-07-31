---
title: "Cancel one bad part, not the whole plate: Klipper's [exclude_object]"
date: 2026-07-31
track: cad-3dprint
summary: "How Klipper's [exclude_object] lets you drop a single failed part from a multi-object plate mid-print — the G-code markers, the printer.cfg one-liner, the Moonraker angle, and the cancel command."
reading_time: 5
tags: [klipper, 3d-printing, moonraker, mainsail, gcode, slicing]
sources:
  - title: "Exclude Objects — Klipper documentation"
    url: "https://www.klipper3d.org/Exclude_Object.html"
  - title: "Exclude Objects — Mainsail docs"
    url: "https://docs.mainsail.xyz/features/exclude-objects/"
  - title: "Klipper Exclude Object Feature: Setup and Configuration — Obico"
    url: "https://www.obico.io/blog/klipper-exclude-object/"
  - title: "kageurufu/preprocess_cancellation"
    url: "https://github.com/kageurufu/preprocess_cancellation"
  - title: "klipper/docs/Exclude_Object.md (source)"
    url: "https://github.com/Klipper3d/klipper/blob/master/docs/Exclude_Object.md"
---

You slice 20 phone-stand feet onto one plate for an overnight run. At 3 a.m. one of them pops off the bed, gets dragged around, and becomes a wandering blob. On a naive setup that blob is now colliding with the nozzle on every layer, threatening the other 19 good parts — and your only lever is to cancel the entire print. Klipper's `[exclude_object]` gives you a scalpel instead: drop that one part and let the rest finish.

## The problem it solves

A multi-object plate is really one continuous G-code stream. The printer has no concept of "part 7" — it just executes moves and extrusions in order. So when one part fails, there's traditionally nothing to cancel *except* the whole file. `[exclude_object]` fixes this by teaching Klipper which moves belong to which named object, so it can skip the ones you've written off while continuing everything else.

## How it works

The slicer wraps each object's toolpath in marker comments/commands, and Klipper acts on them:

- `EXCLUDE_OBJECT_DEFINE` — declared once near the top of the file, one per object. It carries a `NAME`, a `CENTER`, and a bounding `POLYGON`. The Klipper docs' own example:
  ```
  EXCLUDE_OBJECT_DEFINE NAME=calibration_pyramid CENTER=50,50 POLYGON=[[40,40],[50,60],[60,40]]
  ```
- `EXCLUDE_OBJECT_START NAME=<name>` / `EXCLUDE_OBJECT_END` — wrap the block of moves for that object on each layer.

When you exclude an object, Klipper watches for its `EXCLUDE_OBJECT_START` and suppresses the extrusion and printing moves until the matching `EXCLUDE_OBJECT_END`. Non-excluded objects keep printing normally. Because the exclusion is by name, it applies on every subsequent layer automatically — you cancel once, not per layer.

## Enabling it in printer.cfg

The config section is literally just the header — no required parameters:

```ini
# printer.cfg
[exclude_object]
```

Restart Klipper (`RESTART` / `FIRMWARE_RESTART`) and the `EXCLUDE_OBJECT*` commands become available. That's the entire firmware side.

## Getting the markers into your G-code

Klipper only reacts to markers that are actually in the file, so something has to put them there. Two paths:

**1. Native slicer support (preferred).** Turn on "Label objects" so the slicer emits the markers directly:
- **PrusaSlicer** (2.7.0+): Print Settings → Output options → enable **Label objects**.
- **OrcaSlicer / Bambu Studio**: in the process/print settings, enable **Label objects** (Others section). OrcaSlicer emits Klipper-style markers when the printer is flagged as Klipper.
- **Cura**: labeling is available via the object-processing behavior and works out of the box for this feature.

**2. Moonraker preprocessing.** If your slicer (IdeaMaker, older SuperSlicer, etc.) doesn't label natively, let Moonraker inject the markers on upload:

```ini
# moonraker.conf
[file_manager]
enable_object_processing: True
```

This is optional and only needed when the slicer can't do it. Note it parses each file on upload, which can be slow on low-power boards.

**The preprocess_cancellation angle.** On something like a Pi Zero, on-the-fly parsing hurts. `preprocess_cancellation` (kageurufu) is a standalone tool that rewrites the G-code *once* — at slice time via a post-processing script, or ahead of upload — so no runtime parsing is needed. Same result, work done up front. It's the reference implementation the feature grew out of.

## Cancelling a part mid-print

Two equivalent ways:

**From the UI.** In Mainsail or Fluidd, open the G-code viewer / object map for the running print. Each labeled object is selectable; click the exclude (X / cross) icon next to the failed part. The printer skips it from the next layer onward.

**From the console / a macro.** The command is:

```
EXCLUDE_OBJECT NAME=phone_foot_7
```

Use the exact name from the `EXCLUDE_OBJECT_DEFINE` lines (check the file header or the UI object list — names are case-sensitive). Related forms from the G-code reference let you list current exclusions, exclude the object currently printing with `CURRENT=1`, or clear the excluded set with `RESET=1`. The plain `NAME=` form is all you need for the knocked-loose-part case.

## Quick sanity check

Before trusting it on a big overnight plate:

1. `[exclude_object]` in printer.cfg, restart.
2. Slice a 2-part test plate with Label objects on.
3. Upload, confirm the two parts appear in Mainsail's object map, start the print.
4. Exclude one part a few layers in — the nozzle should stop laying plastic there while the other part completes.

If the object list is empty, the markers aren't in the file: your slicer isn't labeling and Moonraker's `enable_object_processing` isn't on.

**Try next:** Slice two small cubes side by side with Label objects enabled, start the print, and run `EXCLUDE_OBJECT NAME=<one-cube-name>` from the Mainsail console after layer 5 — watch one cube stop growing while the other finishes.
