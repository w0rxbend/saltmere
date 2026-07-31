---
title: "KAMP: Mesh Only Where You Print"
date: 2026-07-31
track: cad-3dprint
summary: "KAMP makes Klipper probe just the footprint of the current print instead of the whole bed, and drops a purge line right next to it. Here's how it reads the slicer's object bounds and how to wire it up."
reading_time: 5
tags: [klipper, kamp, adaptive-meshing, bed-leveling, purging, 3d-printing]
sources:
  - title: "kyleisah/Klipper-Adaptive-Meshing-Purging (README)"
    url: "https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging"
  - title: "Klipper Bed Mesh documentation"
    url: "https://www.klipper3d.org/Bed_Mesh.html"
  - title: "KAMP Adaptive_Meshing.cfg"
    url: "https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging/blob/main/Configuration/Adaptive_Meshing.cfg"
  - title: "Adaptive Mesh in Klipper (NordFPV)"
    url: "https://nordfpv.com/blogs/news/adaptive-mesh-in-klipper"
---

## The problem with full-bed meshing

A default `BED_MESH_CALIBRATE` probes a fixed grid over the entire bed. On a 300x300mm Voron that might be a 7x7 or 9x9 grid, and every print pays for it, even a 40mm calibration cube tucked in one corner. You spend a minute-plus probing height at dozens of points the nozzle will never travel over, and the interpolation stretches your real print area across a coarse grid.

The insight behind KAMP (Klipper Adaptive Meshing & Purging) is simple: you only need an accurate mesh under the part you're actually printing. Probe a slightly-larger-than-footprint region with the same point density and you get a *denser*, more relevant mesh in *less* time.

## How KAMP knows where the print is

KAMP doesn't guess. It reads the object bounding boxes your slicer wrote into the G-code. When "Label Objects" is on, the slicer emits `EXCLUDE_OBJECT_DEFINE` lines carrying each object's polygon. Klipper's `[exclude_object]` module parses those, and KAMP's replacement `BED_MESH_CALIBRATE` macro walks the defined objects, finds the min/max X and Y across all of them, and rebuilds the mesh region to fit — choosing bicubic interpolation for 6+ points per axis or lagrange for 3+.

That's why three things are non-negotiable:

- `[exclude_object]` defined in `printer.cfg` (KAMP reads its object data).
- `enable_object_processing: True` in `moonraker.conf`, so Moonraker injects object definitions into the file.
- "Label Objects" enabled in the slicer (OrcaSlicer: *Others → G-code output*; PrusaSlicer/SuperSlicer have the same toggle). No labels, no bounding box, no adaptive region.

Because the region is print-specific, an adaptive mesh should never be saved and reused — a different file covers a different area.

## Installation

Clone into your config, symlink the folder, and copy the settings file:

```bash
cd ~
git clone https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging.git
ln -s ~/Klipper-Adaptive-Meshing-Purging/Configuration ~/printer_data/config/KAMP
cp ~/Klipper-Adaptive-Meshing-Purging/Configuration/KAMP_Settings.cfg ~/printer_data/config/KAMP_Settings.cfg
```

Then add one line to `printer.cfg`:

```ini
[include KAMP_Settings.cfg]
```

`KAMP_Settings.cfg` is a menu of commented-out includes — uncomment only what you want:

```ini
[include ./KAMP/Adaptive_Meshing.cfg]   # adaptive BED_MESH_CALIBRATE
[include ./KAMP/Line_Purge.cfg]          # purge line beside the print
[include ./KAMP/Smart_Park.cfg]          # park near the print before purge
#[include ./KAMP/Voron_Purge.cfg]        # blob-style purge (alternative)
```

Remove any existing `BED_MESH_CALIBRATE` override you have — KAMP replaces it.

## Turning it on

Native Klipper already supports the adaptive flag; KAMP wraps it with sensible defaults and the purge logic. In your slicer's Start G-code (or `PRINT_START` macro), call:

```gcode
BED_MESH_CALIBRATE ADAPTIVE=1
```

The base parameters come from Klipper itself: `ADAPTIVE=[0|1]` toggles the behavior, and `ADAPTIVE_MARGIN` (or the `adaptive_margin` setting in `[bed_mesh]`) adds a mm buffer around the object bounds so the mesh isn't cut exactly at the part edge.

## The settings that matter

In `KAMP_Settings.cfg`:

- `variable_mesh_margin: 0` — mm of extra mesh beyond the print footprint. Bump to 5 if your first layer near part edges looks off.
- `variable_fuzz_amount: 0` — randomizes mesh point locations (max recommended 3mm) so you don't wear the same probe spots every print.
- `variable_probe_dock_enable: False` — set `True` for Klicky/Euclid-style dockable probes so KAMP attaches/detaches around probing.

For adaptive purging (`Line_Purge.cfg`), `SMART_PARK` moves the head next to the print, then `LINE_PURGE` lays a primer line hugging the object rather than on a fixed bed edge. Key knobs:

- `variable_purge_height: 0.8` — nozzle Z during the purge.
- `variable_purge_amount: 30` — mm of filament extruded.
- `variable_flow_rate: 12` — purge flow in mm³/s.

One gotcha: `LINE_PURGE` needs `max_extrude_cross_section` of at least 5 in `[extruder]`, or Klipper aborts the fat purge move.

## Try next

**Try next:** Print the same small part twice with `variable_verbose_enable: True`: once with `BED_MESH_CALIBRATE` (full bed) and once with `ADAPTIVE=1`. Watch the console log the probe-point count and region, time each run, then compare the two meshes in Mainsail's viewer to see how much denser the adaptive one is over your actual footprint.
