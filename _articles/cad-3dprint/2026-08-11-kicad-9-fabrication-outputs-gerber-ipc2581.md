---
title: "From KiCad 9 Board to Fab Package: Gerber X2, Excellon, and IPC-2581"
date: 2026-08-11
track: cad-3dprint
summary: "A finished PCB layout is not a manufacturable package. What the fabrication package contains, what structured attributes in Gerber X2 and IPC-2581 carry that bare RS-274X cannot, and how KiCad 9 Output Jobsets with kicad-cli make the export reproducible."
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

**Gist.** A routed `.kicad_pcb` is a design database; a board house builds from a package of machine-readable image, drill and assembly files, and the translation between the two is where a revision silently loses information. Structured formats — **Gerber X2**, which embeds per-aperture attributes, and **IPC-2581**, which carries the whole package in one Extensible Markup Language (XML) document — preserve net, pad and stackup semantics that flat RS-274X discards, so the fab's computer-aided manufacturing (CAM) tooling can verify rather than infer. The cost is that the package is now a derived artifact of many coupled export settings, which is why KiCad 9 pushes the definition into a checked-in jobset file rather than a sequence of dialogs.

## What the package must answer

A fabrication package answers a small set of blunt questions: where the copper is, where the mask opens, where holes go, and what shape the board is cut to. For a two-layer board that resolves to:

- **Copper layers** — one image per layer (`F.Cu`, `B.Cu`, plus any inner layers). The etch pattern.
- **Solder mask** (`F.Mask`, `B.Mask`) — the coating, with openings where pads sit.
- **Silkscreen** (`F.Silkscreen`, `B.Silkscreen`) — reference designators and outlines.
- **Solder paste** (`F.Paste`, `B.Paste`) — stencil apertures, required only when a stencil or assembly is ordered.
- **Board outline** (`Edge.Cuts`) — the mechanical profile the router follows. **Omitting this layer leaves the fab with no defined board shape**, and the order cannot proceed without a clarification round.
- **Drill files** — hole positions and diameters, plated and non-plated, conventionally Excellon.

Historically each image went out as **RS-274X**: a flat vector image description with no concept of a net or a component. It is sufficient to etch from, but it discards everything the design database knew. A fab that needs to distinguish pad flashes from trace segments, or that wants to compare the copper against the intended netlist, cannot recover that from RS-274X — the information is not encoded.

## What X2 and IPC-2581 add

**Gerber X2** is the same underlying file format carrying structured attributes. An aperture can be marked as a component pad, associated with a net, and a drill marked plated or non-plated; layers carry their function and position in a defined stack. CAM tooling reads those attributes to assign layers automatically and to run a netlist comparison against the copper instead of inferring intent from geometry. **KiCad emits X2 by default; disabling it requires the explicit `--no-x2` flag.**

**IPC-2581** is a single open, vendor-neutral XML document that bundles copper, mask, silkscreen, drill, stackup, netlist *and* assembly data — component placement and bill-of-materials (BOM) references — together. The practical difference is one of coupling: a zip of a dozen Gerbers plus a separate drill file plus a placement comma-separated values (CSV) file plus a BOM is four independently revisable artifacts, and **any one of them can be regenerated while the others go stale**. IPC-2581 collapses them into one file with one revision. **KiCad 9 exports IPC-2581 revision B or C directly.**

Neither format is universally accepted, so the operative rule is: X2 Gerbers where the fab expects Gerbers, IPC-2581 where the fab accepts it.

## The kicad-cli commands

Each fabrication output has a `kicad-cli` subcommand, which is what makes the package reproducible in continuous integration.

```bash
# 1. Gerber X2 copper/mask/silk/paste/outline (X2 + netlist attributes on by default)
kicad-cli pcb export gerbers \
  --output fab/gerbers/ \
  --layers F.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,B.Paste,Edge.Cuts \
  board.kicad_pcb

# 2. Excellon drill files plus a human-readable drill map
kicad-cli pcb export drill \
  --output fab/gerbers/ \
  --format excellon --excellon-units mm \
  --generate-map --map-format pdf \
  board.kicad_pcb

# 3. IPC-2581 — the whole package in one compressed XML document
kicad-cli pcb export ipc2581 \
  --output fab/board.xml --version C --units mm --compress \
  board.kicad_pcb

# 4. Component placement (CPL / pick-and-place) for assembly
kicad-cli pcb export pos \
  --output fab/board-cpl.csv \
  --side both --format csv --units mm \
  board.kicad_pcb
```

The BOM derives from the **schematic**, not the board, and therefore takes a `.kicad_sch` input:

```bash
kicad-cli sch export bom \
  --output fab/board-bom.csv \
  --fields "Reference,Value,Footprint,\${QUANTITY},LCSC" \
  --labels "Designator,Comment,Footprint,Quantity,LCSC Part #" \
  --group-by "Value,Footprint" \
  --exclude-dnp \
  board.kicad_sch
```

The `--labels` values are the column headers JLCPCB's assembly form expects: the `Designator`/`Comment`/`Footprint` trio plus an LCSC part column. Removing the `LCSC` field yields a generic BOM. `--group-by "Value,Footprint"` collapses parts that share both attributes into one line with a quantity; **two components with the same value but different footprints remain separate rows**, because the grouping key includes the footprint. `--exclude-dnp` drops do-not-populate parts, so the BOM and the placement file must be generated under consistent assumptions or **the assembler receives coordinates for parts that were never ordered**.

Board houses publish recommended plot settings for the graphical Plot dialog; PCBWay's KiCad 9 guide is one such walkthrough. The command-line exporter honours the same board settings stored in the project, so the presets are configured once and the invocation stays short.

## Output Jobsets

Five separate commands per revision is a state that drifts: one is edited, another is not, and the package that ships is a mixture of two board revisions. KiCad 9 introduced **Output Jobsets** to make the package a single declared object. A jobset is a `.kicad_jobset` file, created through **File → New Jobset File** in the project manager, holding a list of *jobs* — export gerbers, export drill, export IPC-2581, export BOM, run design rule check (DRC), render a STEP model — grouped into *destinations*, each destination being an output folder or a zip archive. Layers, formats and presets are configured once; the file is checked into the repository beside the board.

Because the jobset lives in the project, it is the single definition of what a release contains, and it can be executed from the graphical interface or headless:

```bash
# Generate every destination in the jobset
kicad-cli jobset run --file fab.kicad_jobset board.kicad_pro

# Or a single destination, failing the build on any job error
kicad-cli jobset run --file fab.kicad_jobset \
  --output JLCPCB --stop-on-error board.kicad_pro
```

**`--stop-on-error` is what converts the jobset from a convenience into a gate**: without it a failed job leaves a partial destination that still looks like a complete package. Pairing the run with a scripted DRC job ([KiCad Custom DRC Rules](/articles/cad-3dprint/2026-07-31-kicad-9-custom-drc-rules)) makes a rule violation block the release. Operations the jobset and CLI do not express — reading net data back, mutating footprints programmatically — belong to the [IPC Python API (kipy)](/articles/cad-3dprint/2026-08-10-kicad-9-ipc-api-kipy); the wider automation surface is covered in [Automating KiCad 9](/articles/cad-3dprint/2026-07-25-kicad-9-scripting).

## Pitfalls

- **`Edge.Cuts` omitted from the `--layers` list.** The package plots and zips without error; the fab has no board profile and has to query the order.
- **`--no-x2` set, or an X2-unaware CAM flow.** Layer assignment and netlist comparison fall back to geometric inference, so a swapped or duplicated layer image is no longer caught automatically.
- **BOM and placement generated with different `--exclude-dnp` settings.** The placement file contains coordinates for parts absent from the BOM, and assembly stalls on unmatched designators.
- **BOM regenerated from the schematic while Gerbers come from an older board export.** Nothing in a Gerber-plus-CSV package cross-checks revisions; IPC-2581 avoids the class of failure by carrying both in one document.
- **`jobset run` without `--stop-on-error`.** A failing job leaves the destination folder or archive partially populated, and the artifact is shipped as if complete.
- **Drill map treated as a machine input.** The `--generate-map` output is a human-readable drawing; the Excellon files are what the drilling machine consumes.
