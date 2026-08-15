---
title: "KAMP: Meshing Only the Printed Footprint"
date: 2026-07-31
track: cad-3dprint
summary: "KAMP makes Klipper probe the footprint of the current print instead of the whole bed, and places a purge line beside it. How the macro reads the slicer's object bounds, and what the configuration requires."
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

**Gist.** A default `BED_MESH_CALIBRATE` probes a fixed grid across the whole bed, so a part occupying a small corner still pays the full probing time and receives its height correction from an interpolation stretched over the entire build surface. KAMP (Klipper Adaptive Meshing & Purging) replaces that macro with one that reads the object bounding boxes the slicer wrote into the G-code and rebuilds the mesh region to cover only those bounds plus a margin, and it moves the priming purge to the edge of the same region. The cost is a dependency chain — slicer labelling, Moonraker object processing, and Klipper's `[exclude_object]` module all have to be enabled — and a mesh that is valid for exactly one G-code file and must never be saved and reused.

## Why the full-bed grid is the wrong grid

A bed mesh is a set of probed Z heights on a rectangular grid, with values between grid points supplied by interpolation. On a 300x300 mm bed a **7x7 or 9x9 grid** is a common configuration, meaning 49 to 81 probe points spanning the full 300 mm in each axis. Two consequences follow from that geometry.

The first is time. Every probe point costs a travel move plus one or more probing cycles, and the count is fixed regardless of what is being printed. A 40 mm calibration cube in one corner triggers the same probing sequence as a part filling the bed.

The second, and the one that affects print quality, is **spatial resolution**. With a 9x9 grid over 300 mm the grid pitch is roughly 37 mm. A 40 mm part therefore spans about one grid interval per axis, so nearly all of its first layer height correction is interpolated rather than measured. Probing the same number of points over a region the size of the part instead reduces the pitch to a few millimetres, and every value the nozzle follows is close to a measurement.

The adaptive mesh does not add probe points. It **relocates them**, trading coverage of bed area the nozzle will never visit for density under the area it will.

## How the object bounds reach the macro

KAMP does not infer the footprint from the toolpath. It reads bounding data that the slicer places in the file, and that data travels through a fixed chain:

1. **The slicer emits object definitions.** With the "Label Objects" option enabled, the slicer writes `EXCLUDE_OBJECT_DEFINE` lines carrying each object's polygon. OrcaSlicer, PrusaSlicer and SuperSlicer all expose this as a "Label objects" checkbox in their print-settings output options.
2. **Moonraker processes the file.** `enable_object_processing: True` in `moonraker.conf` makes Moonraker inject object definitions into the uploaded G-code.
3. **Klipper parses them.** The `[exclude_object]` module in `printer.cfg` consumes the `EXCLUDE_OBJECT_DEFINE` commands and holds the resulting object data.
4. **KAMP reads that data.** Its replacement `BED_MESH_CALIBRATE` macro walks the defined objects, takes the **minimum and maximum X and Y across all of them**, and rebuilds the mesh region from that rectangle.

The chain has no fallback that recovers the footprint by other means. If any link is missing — no label option, no object processing, no `[exclude_object]` — there are no object definitions, so there is no bounding box and no adaptive region.

Having sized the region, the macro must also keep the resulting point count compatible with the interpolation algorithm, because **Klipper's two algorithms have opposite constraints**: `bicubic` needs a minimum number of probed points per axis, while `lagrange` is capped at a small maximum. Shrinking the region without adjusting the point count can therefore land outside the range one of them accepts.

Because the region is derived from one specific file's objects, **an adaptive mesh must not be saved and reloaded**. A saved profile describes the rectangle occupied by the print that generated it; a later print with a different footprint would be corrected using heights measured somewhere else.

## Installation and the include structure

The repository is cloned once and exposed to Klipper's configuration directory by symlink, so that updating the clone updates the macros:

```bash
cd ~
git clone https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging.git
ln -s ~/Klipper-Adaptive-Meshing-Purging/Configuration ~/printer_data/config/KAMP
cp ~/Klipper-Adaptive-Meshing-Purging/Configuration/KAMP_Settings.cfg ~/printer_data/config/KAMP_Settings.cfg
```

`KAMP_Settings.cfg` is copied rather than symlinked because it is the file the operator edits; the symlinked `KAMP` directory holds the macros themselves.

A single line in `printer.cfg` pulls in the settings file:

```ini
[include KAMP_Settings.cfg]
```

`KAMP_Settings.cfg` is a menu of commented-out includes. Each feature is opted into separately:

```ini
[include ./KAMP/Adaptive_Meshing.cfg]   # adaptive BED_MESH_CALIBRATE
[include ./KAMP/Line_Purge.cfg]          # purge line beside the print
[include ./KAMP/Smart_Park.cfg]          # park near the print before purge
#[include ./KAMP/Voron_Purge.cfg]        # blob-style purge (alternative)
```

Because `Adaptive_Meshing.cfg` defines a macro named `BED_MESH_CALIBRATE`, **any pre-existing `BED_MESH_CALIBRATE` override in the configuration must be removed**; two definitions of the same macro name conflict.

## Invoking the adaptive path

Adaptive meshing is a Klipper feature; KAMP wraps it with defaults and adds the purge logic. The call belongs in the slicer's start G-code or in the `PRINT_START` macro:

```gcode
BED_MESH_CALIBRATE ADAPTIVE=1
```

The base parameters come from Klipper itself. `ADAPTIVE=[0|1]` toggles the behaviour, and `ADAPTIVE_MARGIN` — or the `adaptive_margin` setting in the `[bed_mesh]` section — adds a millimetre buffer around the object bounds so the mesh does not terminate exactly at the part edge, where the nozzle still needs a corrected height.

## Settings that change behaviour

In `KAMP_Settings.cfg`:

- `variable_mesh_margin: 0` — millimetres of mesh beyond the print footprint. Raising it is the remedy when the first layer degrades near part edges, where the mesh would otherwise stop.
- `variable_fuzz_amount: 0` — randomises mesh point locations by a small distance, so successive prints do not probe the identical spots on the build surface.
- `variable_probe_dock_enable: False` — set to `True` for dockable probes such as Klicky or Euclid, so that KAMP attaches and detaches the probe around the probing sequence.

`Line_Purge.cfg` supplies the adaptive purge. `SMART_PARK` moves the toolhead adjacent to the print, and `LINE_PURGE` then lays the primer line hugging the object rather than along a fixed bed edge. The relevant variables:

- `variable_purge_height: 0.8` — nozzle Z during the purge.
- `variable_purge_amount: 30` — millimetres of filament extruded.
- `variable_flow_rate: 12` — purge flow in mm³/s.

A purge at that height and flow is a single extrusion move with a large cross-section, which collides with Klipper's extrusion sanity check: `LINE_PURGE` requires **`max_extrude_cross_section` of at least 5** in the `[extruder]` section, otherwise Klipper aborts the move.

## Measuring the difference

Setting `variable_verbose_enable: True` makes the macro log the probe-point count and the computed region to the console. Printing the same small part twice — once with a plain `BED_MESH_CALIBRATE` over the full bed, once with `ADAPTIVE=1` — gives both the probing time for each run and two meshes that can be compared in Mainsail's viewer over the footprint the part occupies.

## Pitfalls

- **Saving an adaptive mesh and loading it for a later print applies heights measured over a different rectangle.** The region is derived from one file's object bounds; the profile carries no record that it is footprint-specific.
- **"Label Objects" disabled in the slicer produces no `EXCLUDE_OBJECT_DEFINE` lines, so the macro has no bounding box.** The chain has no fallback that reconstructs the footprint from the toolpath.
- **`enable_object_processing: True` missing from `moonraker.conf` breaks the same chain one link later**, even when the slicer option is on, because Moonraker is what injects the definitions into the uploaded file.
- **`LINE_PURGE` aborts when `max_extrude_cross_section` is below 5.** The purge is one wide extrusion move and trips Klipper's extrusion sanity check at the default value.
- **Leaving an older `BED_MESH_CALIBRATE` override in the configuration conflicts with the macro of the same name that `Adaptive_Meshing.cfg` defines.**
- **A large `variable_fuzz_amount` displaces probe points relative to a small adaptive region**, so a point can be moved away from the part the mesh is meant to describe.
- **A dockable probe with `variable_probe_dock_enable: False` is not attached or detached by KAMP**, so the probing sequence runs without the docking moves the hardware requires.
