---
title: "Automating KiCad 9: kicad-cli fab outputs and the new IPC Python API"
date: 2026-07-25
track: cad-3dprint
summary: "KiCad 9 makes board work scriptable in two ways that matter for a hobby ESP32 project: reproducible fabrication outputs from one command line, and a new IPC-based Python API that's replacing the old pcbnew wrapper."
reading_time: 5
tags: [kicad, kicad-cli, pcb, python]
sources:
  - title: "KiCad 9.0.0 Released"
    url: "https://www.kicad.org/blog/2025/02/Version-9.0.0-Released/"
  - title: "KiCad Command-Line Interface (9.0 docs)"
    url: "https://docs.kicad.org/9.0/en/cli/cli.html"
  - title: "KiCad IPC API (Developer Documentation)"
    url: "https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html"
  - title: "kicad-python (kipy) API reference"
    url: "https://docs.kicad.org/kicad-python-main/kicad.html"
  - title: "CNX Software: KiCad 9 release"
    url: "https://www.cnx-software.com/2025/02/21/kicad-9-release/"
---

KiCad **9.0.0** shipped on **20 February 2025**, and for anyone laying out a small ESP32/IoT board the interesting part isn't a new feature in the GUI — it's that the boring, error-prone steps (generating Gerbers, drill files, a BOM, running DRC) are now fully scriptable and reproducible. Two mechanisms do the work: `kicad-cli` for batch outputs, and a brand-new IPC-based Python API that's beginning to replace the old `pcbnew` SWIG wrapper.

## Fabrication outputs from the command line

Every board maker wants Gerbers, an Excellon drill file, and a BOM. Clicking through export dialogs is how you end up with a fab package that doesn't match the commit you tagged. `kicad-cli` (installed alongside KiCad 9) makes it a script you can run in CI:

```bash
# Gerbers for the copper + mask + silk + edge layers
kicad-cli pcb export gerbers \
  --output fab/ \
  --layers F.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,Edge.Cuts \
  board.kicad_pcb

# Excellon drill file next to them
kicad-cli pcb export drill --format excellon --output fab/ board.kicad_pcb

# Bill of materials straight from the schematic
kicad-cli sch export bom --output bom.csv board.kicad_sch
```

Because it's the same design file the GUI uses, the output is deterministic — tag `v1.2`, re-run the script, and you get byte-for-byte the package you sent last time.

## Gate your board on DRC

The command that saves real money is the design rule check. Run it headless and fail the build on violations before you pay for fabrication:

```bash
kicad-cli pcb drc --severity-all --format json --output drc.json board.kicad_pcb
```

KiCad 9 also strengthened DRC itself: a new **creepage** check for electrical clearance, **component classes** you can write rules against, and custom violations you raise from text variables like `${DRC_ERROR}`. Wire the `drc.json` exit into a Makefile or GitHub Action and a shorted net can never reach the fab house.

## The API transition: kicad-python (kipy)

For anything interactive — placing a repeated connector footprint, bulk-editing net classes, reading pad positions — KiCad 9 introduces the **IPC API**. Instead of loading your script inside KiCad's Python interpreter (the legacy `import pcbnew` SWIG wrapper), it talks to a *running* KiCad over a local socket using Protocol Buffers, which is far more stable across versions. The client library is `kicad-python`, imported as `kipy`:

```bash
pip install kicad-python
```

```python
from kipy import KiCad

kicad = KiCad()            # connects to the running KiCad instance
board = kicad.get_board()  # the PCB currently open in the editor

for fp in board.get_footprints():
    ref = fp.reference_field.text.value
    pos = fp.position
    print(ref, pos.x, pos.y)
```

In 9.0 the IPC API's scope is roughly equivalent to the old Action Plugins, and `pcbnew` still works — but the project has stated the IPC interface will eventually replace the SWIG bindings, so new automation is worth writing against `kipy`. Note the mental shift: your script is now a *client* of an open editor, not code running inside it.

## Where this pays off for an ESP32 board

A typical IoT node is a few of the same footprint repeated — decoupling caps, headers, mounting holes. Between KiCad 9's **multichannel design** (repeat a laid-out block) in the GUI and a `kipy` script for the tedious edits, plus a `kicad-cli` fab-and-DRC step in CI, your board becomes as reproducible as your firmware build.

**Try next:** write a three-line shell script that runs `kicad-cli pcb drc` and exits non-zero on any violation, then call it from your project's CI. A board that can't be fabricated until DRC is clean is the PCB equivalent of a failing unit test.
