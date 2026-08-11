---
title: "From KiCad 9 Board to Fab Package: Gerber X2, Excellon, and IPC-2581"
date: 2026-08-11
track: cad-3dprint
summary: "A finished PCB is only half the job — the fab needs a package it can actually build. Here's what goes in it, why Gerber X2 and IPC-2581 beat bare RS-274X, and how KiCad 9's Output Jobsets plus kicad-cli make the whole thing reproducible."
reading_time: 6
tags: [kicad, pcb, gerber, ipc-2581, fabrication, kicad-cli]
sources:
  - title: "KiCad Command-Line Interface (9.0 docs)"
    url: "https://docs.kicad.org/9.0/en/cli/cli.html"
  - title: "KiCad — Version 9.0.0 Released (Feb 20, 2025)"
    url: "https://www.kicad.org/blog/2025/02/Version-9.0.0-Released/"
  - title: "How to Export Gerber, BOM, and Pick-and-Place Files in KiCad 9.0 — PCBWay"
    url: "https://www.pcbway.com/helpcenter/generate_gerber/How_to_Export_Gerber__BOM__and_Pick_and_Place_Files_in_KiCad_9_0.html"
  - title: "How to Export IPC-2581 Files from KiCad — PCBSync"
    url: "https://pcbsync.com/how-to-export-ipc-2581-files-from-kicad/"
  - title: "KiCad 9 Jobsets: Automating Your Design Outputs — MicroType"
    url: "https://www.microtype.io/blog/kicad-9-jobsets"
---

Routing the last trace is a milestone, not the finish line. What a board house actually builds from is a *package* of machine-readable files, and the gap between "my layout looks done" and "the fab accepted it" is where most first-time projects lose a day. This is the export step: turning a `.kicad_pcb` and its schematic into something a manufacturer can quote, fabricate, and assemble without emailing you back.

## What the fab actually needs

Strip away the acronyms and a fabrication package answers a few blunt questions: where is the copper, where is the mask opening, where do holes go, and how big is the board? Concretely, for a standard two-layer board that means:

- **Copper layers** — one image per layer (`F.Cu`, `B.Cu`, and any inner layers). This is the etch pattern.
- **Solder mask** (`F.Mask`, `B.Mask`) — the green (or whatever) coating, with openings where pads sit.
- **Silkscreen** (`F.Silkscreen`, `B.Silkscreen`) — the white reference designators and outlines.
- **Solder paste** (`F.Paste`, `B.Paste`) — stencil apertures, only needed if you're getting a stencil or assembly.
- **Board outline** (`Edge.Cuts`) — the mechanical shape the router follows. Miss this and the fab can't tell your board from a rectangle.
- **Drill files** — hole positions and diameters, plated and non-plated, usually as Excellon.

Historically each of those went out as an **RS-274X** Gerber: a flat, dumb image format with no idea what a "net" or a "component" is. It works, but it throws away everything KiCad knows. If the fab needs to know which apertures are pads versus traces, or wants to verify your netlist against the copper, RS-274X can't tell them.

## Why Gerber X2 and IPC-2581 are better

**Gerber X2** is the same file format with structured attributes embedded in it. Each aperture carries metadata — this flash is a *component pad* on this *net*, this drill is *plated*, this layer is *top copper* in a defined stack. A fab's CAM tooling reads those attributes to auto-assign layers and run netlist comparison instead of guessing. KiCad emits X2 by default; you have to opt *out* with `--no-x2`. There's rarely a reason to.

**IPC-2581** goes further: it's a single XML file that bundles the entire package — copper, mask, silk, drill, stackup, netlist, *and* assembly data (component placement, BOM references) — in one open, vendor-neutral document. No zip of a dozen Gerbers plus a separate drill file plus a CSV that might drift out of sync. One file, one revision, fab and assembly data traveling together. KiCad 9 exports IPC-2581 revision B or C directly, and turnkey houses increasingly prefer it because there's nothing to reconcile.

The practical takeaway: send X2 Gerbers when your fab expects Gerbers, send IPC-2581 when they accept it, and stop hand-assembling zips.

## The kicad-cli commands

Every fabrication output KiCad can produce has a `kicad-cli` subcommand, which is what makes this scriptable in CI. The core four:

```bash
# 1. Gerber X2 copper/mask/silk/paste/outline (X2 + netlist on by default)
kicad-cli pcb export gerbers \
  --output fab/gerbers/ \
  --layers F.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts \
  board.kicad_pcb

# 2. Excellon drill files + a human-readable drill map
kicad-cli pcb export drill \
  --output fab/gerbers/ \
  --format excellon --excellon-units mm \
  --generate-map --map-format pdf \
  board.kicad_pcb

# 3. IPC-2581 — the whole package in one compressed XML
kicad-cli pcb export ipc2581 \
  --output fab/board.xml --version C --units mm --compress \
  board.kicad_pcb

# 4. Component placement (CPL / pick-and-place) for assembly
kicad-cli pcb export pos \
  --output fab/board-cpl.csv \
  --side both --format csv --units mm \
  board.kicad_pcb
```

And the BOM, which comes from the *schematic*, not the board:

```bash
kicad-cli sch export bom \
  --output fab/board-bom.csv \
  --fields "Reference,Value,Footprint,\${QUANTITY},LCSC" \
  --labels "Designator,Comment,Footprint,Quantity,LCSC Part #" \
  --group-by "Value,Footprint" \
  --exclude-dnp \
  board.kicad_sch
```

Those `--fields` and `--labels` names are exactly the columns JLCPCB's assembly form expects — the `Designator`/`Comment`/`Footprint` header trio and an LCSC part column. Drop the `LCSC` field and this is a generic BOM for any house. Both JLCPCB and PCBWay ship recommended plot presets in the GUI's Plot dialog; the CLI honors the same board settings, so configure once in the project and the command line stays terse.

## Output Jobsets: define once, re-run forever

Typing five commands per revision is exactly the kind of thing you get subtly wrong at 1am. KiCad 9 introduced **Output Jobsets** to fix it. A jobset is a `.kicad_jobset` file — created via **File → New Jobset File** in the project manager — that captures a list of *jobs* (export gerbers, export drill, export IPC-2581, export BOM, run DRC, render a STEP model) grouped into *destinations* (an output folder, or a zip archive). You configure the layers, formats, and presets once in the GUI, and the whole set becomes a reproducible artifact checked into the repo alongside the board.

Because the jobset lives in the project, it's the single source of truth for "what a release looks like." Run it from the GUI, or run the identical set headless in CI:

```bash
# Generate every destination in the jobset
kicad-cli jobset run --file fab.kicad_jobset board.kicad_pro

# Or just the "JLCPCB" destination, and fail the build on any job error
kicad-cli jobset run --file fab.kicad_jobset \
  --output JLCPCB --stop-on-error board.kicad_pro
```

Drop that line in a GitHub Action and every tagged revision produces a fresh, identical fab package with zero manual clicks. Pair it with a scripted DRC gate ([KiCad Custom DRC Rules](/articles/cad-3dprint/2026-07-31-kicad-9-custom-drc-rules)) so a rule violation blocks the release, and you have a fabrication pipeline that's as version-controlled as your firmware. For anything the jobset and CLI can't express — reading net data back, mutating footprints programmatically — reach for the [IPC Python API (kipy)](/articles/cad-3dprint/2026-08-10-kicad-9-ipc-api-kipy); for the broader automation story see [Automating KiCad 9](/articles/cad-3dprint/2026-07-25-kicad-9-scripting).

**Try next:** Create a `.kicad_jobset` with three destinations — a JLCPCB zip, a PCBWay zip, and an IPC-2581 file — then wire `kicad-cli jobset run --stop-on-error` into CI behind a passing DRC.
