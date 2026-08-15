---
title: "Cancelling one part of a plate: Klipper's [exclude_object]"
date: 2026-07-31
track: cad-3dprint
summary: "How Klipper's [exclude_object] drops a single failed part from a multi-object plate mid-print — the G-code markers, the printer.cfg section, the Moonraker preprocessing path, and the cancel command."
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

**Gist.** A plate of twenty parts is a single continuous G-code stream, so a firmware that only tracks moves has exactly one remedy when one part detaches and becomes a nozzle-dragged blob: cancel the file. Klipper's `[exclude_object]` module removes that all-or-nothing choice by consuming marker commands that name which moves belong to which object, and suppressing the moves of objects that have been excluded. The cost is that the markers must exist in the file — either the slicer emits them, or something parses and rewrites the file before printing.

## What the firmware lacks without the markers

G-code carries geometry, not identity. A move to `X104.3 Y88.1 E0.0142` says nothing about which of the arranged objects it belongs to; the association exists only in the slicer, which discards it when it serialises the toolpath. Any mid-print "skip this one" therefore requires the slicer's knowledge to survive into the file. `[exclude_object]` defines the vocabulary that carries it.

## The marker vocabulary

Two families of commands appear in a labelled file.

- `EXCLUDE_OBJECT_DEFINE` — emitted once per object, near the top of the file. It carries `NAME`, and optionally a `CENTER` and a bounding `POLYGON`. A definition line has the form:

  ```
  EXCLUDE_OBJECT_DEFINE NAME=calibration_pyramid CENTER=50,50 POLYGON=[[40,40],[50,60],[60,40]]
  ```

  The definitions are what a front end lists and draws; `CENTER` and `POLYGON` give the interface a shape to render and click on.

- `EXCLUDE_OBJECT_START NAME=<name>` and `EXCLUDE_OBJECT_END` — a matched pair wrapping the block of moves belonging to that object, **repeated on every layer** the object occupies.

**The name is the identity.** Exclusion is recorded against the name, not against a file offset or a layer index, which is why a single `EXCLUDE_OBJECT` call holds for the remainder of the print: every later `EXCLUDE_OBJECT_START` bearing that name enters the suppressed state again. The name given to `EXCLUDE_OBJECT` has to be the one carried by the `EXCLUDE_OBJECT_DEFINE` lines.

## The suppression state machine

Klipper's behaviour reduces to a small state machine over the stream. The module maintains a **set of excluded names**. On `EXCLUDE_OBJECT_START NAME=n`, if `n` is in that set, the module enters a suppressing state; extrusion and printing moves are not executed until the matching `EXCLUDE_OBJECT_END`. If `n` is not in the set, the block executes normally. Moves outside any `START`/`END` pair — the start G-code, layer changes, the end G-code — are never suppressed, because they are not attributed to any object.

The consequence worth stating plainly: **exclusion takes effect from the next `EXCLUDE_OBJECT_START` for that name onward.** Material already deposited on the current layer stays where it is, and a part excluded halfway through its own block finishes that block. The blob is not removed; only further growth of it stops.

## Enabling the module

The configuration section takes no required parameters:

```ini
# printer.cfg
[exclude_object]
```

After a `RESTART` or `FIRMWARE_RESTART`, the `EXCLUDE_OBJECT*` commands are registered. That is the whole firmware-side change; nothing about kinematics, extrusion or the object list is configured here.

## Getting markers into the file

Klipper reacts only to markers present in the stream, so one of two producers has to supply them.

**Slicer labelling.** Enabling the slicer's object-labelling option makes it emit the markers directly:

- **PrusaSlicer**: Print Settings → Output options → **Label objects**.
- **OrcaSlicer / Bambu Studio**: **Label objects** in the process/print settings. OrcaSlicer emits Klipper-style markers when the printer profile is set to the Klipper G-code flavour.
- **Cura**: no equivalent native option is documented by the sources cited here; files sliced with Cura generally reach Klipper through one of the rewriting paths below.

**Moonraker preprocessing.** For slicers that do not emit the markers natively, Moonraker can inject them when the file is uploaded:

```ini
# moonraker.conf
[file_manager]
enable_object_processing: True
```

This path **parses every uploaded file**, so upload latency scales with file size and with the host's CPU. On low-power single-board hosts that cost is noticeable, and it is paid again on every re-upload of the same file.

**Ahead-of-time rewriting.** `preprocess_cancellation` (kageurufu) is a standalone tool that performs the same rewrite once — as a slicer post-processing script, or before upload — so no parsing happens on the printer host. It emits the same marker vocabulary; Moonraker's own object-processing path is built on it.

## Excluding a part mid-print

**From a front end.** Mainsail and Fluidd render the object map for the running print from the `EXCLUDE_OBJECT_DEFINE` polygons. Each object is selectable, and the exclude control marks it. This is a wrapper over the same command.

**From the console or a macro.**

```
EXCLUDE_OBJECT NAME=phone_foot_7
```

The G-code reference documents further forms: listing the current exclusions, excluding the object currently printing with `CURRENT=1`, and clearing the excluded set with `RESET=1`. The plain `NAME=` form covers the detached-part case.

## Verification procedure

Confirming the chain before an unattended multi-part run costs one short print:

1. Add `[exclude_object]` to printer.cfg and restart.
2. Slice a two-part plate with object labelling enabled.
3. Upload, and confirm both objects appear in the front end's object map.
4. Start the print, exclude one object a few layers in, and observe that extrusion continues on the other alone.

An empty object map is the diagnostic signal: the file carries no `EXCLUDE_OBJECT_DEFINE` lines, meaning the slicer did not label and `enable_object_processing` is not enabled.

## Pitfalls

- **The object map is empty and `EXCLUDE_OBJECT` reports an unknown name.** The uploaded file has no markers: object labelling is off in the slicer and Moonraker preprocessing is disabled. The firmware side is fine and restarting Klipper changes nothing.
- **`EXCLUDE_OBJECT NAME=...` is rejected for an object visible in the UI.** The name has to match the one in the `EXCLUDE_OBJECT_DEFINE` line; a front end may render a shortened or prettified label, so a name transcribed by eye from the object map is a common mismatch.
- **The excluded part keeps growing for the rest of the current layer.** Suppression begins at the next `EXCLUDE_OBJECT_START` for that name, so a block already in progress runs to its `EXCLUDE_OBJECT_END`.
- **Excluding a part does not remove the debris.** The detached part and any blob remain on the bed and can still be struck by the nozzle or by moves belonging to other objects.
- **Uploads become slow after enabling `enable_object_processing`.** Every file is parsed on upload; on a low-power host, moving the rewrite to `preprocess_cancellation` at slice time removes that per-upload cost.
- **A file sliced before labelling was enabled stays unlabelled.** Re-slicing is required; toggling the setting does not retroactively affect G-code already produced or already uploaded.
- **`RESET=1` clears the whole excluded set.** Objects excluded earlier in the print resume printing on their next block, which is rarely the intent when only one entry needed correcting.
