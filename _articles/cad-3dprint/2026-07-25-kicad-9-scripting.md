---
title: "Automating KiCad 9: kicad-cli fab outputs and the IPC Python API"
date: 2026-07-25
track: cad-3dprint
summary: "KiCad 9 makes board work scriptable in two ways: reproducible fabrication outputs from one command line, and an IPC-based Python API that is replacing the pcbnew SWIG wrapper."
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

**Gist.** A fabrication package produced by clicking through export dialogues is not reproducible: nothing ties the exported Gerbers, drill file and bill of materials (BOM) to a particular revision of the design files. KiCad **9.0.0**, released **20 February 2025**, addresses this with two separate mechanisms — `kicad-cli`, a headless batch exporter that reads the same `.kicad_pcb` and `.kicad_sch` files the graphical editor reads, and an inter-process communication (IPC) application programming interface (API) that lets an external Python process manipulate a board over a local socket. The cost is a split model: the command-line path is stateless and runs without a running editor, while the IPC path requires a live KiCad instance with the board already open, so the two cannot be used interchangeably in continuous integration (CI).

## Fabrication outputs from the command line

`kicad-cli` is installed alongside KiCad 9 and exposes the export routines the graphical user interface (GUI) calls, grouped by document type: `kicad-cli pcb …` operates on a board file, `kicad-cli sch …` on a schematic.

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

The load-bearing property is that **the layer set is named explicitly on the command line rather than remembered as editor state**. In the GUI, the selected plot layers are part of the project's saved settings, so an export performed months apart can differ without the board having changed. In the script above, the layer list is text in a file under version control: **re-running the same command against the same commit of `board.kicad_pcb` regenerates the same output set**, because both the input geometry and the export parameters are versioned together. Nothing about the mechanism requires the editor to be open, which is what makes it usable from a build job.

## Gating a build on the design rule check

The design rule check (DRC) is the step whose failure costs money, because a violation that reaches the fabricator is discovered only after the panels are made. `kicad-cli` runs it headless and emits a machine-readable report:

```bash
kicad-cli pcb drc --severity-all --format json --output drc.json board.kicad_pcb
```

`--severity-all` selects every severity class rather than errors alone, and `--format json` produces a report a build script can parse instead of a human-readable listing. The useful invariant is that **the artefact-producing job and the checking job read the same board file**, so a clean DRC result cannot describe a different revision than the one whose Gerbers were uploaded — provided both commands run in the same checkout.

KiCad 9 also extended the rule engine itself. A **creepage** check evaluates clearance measured along surfaces, which is distinct from the existing clearance check through air or across a board edge. **Component classes** group footprints so a rule can be written against the class rather than enumerating references, and custom rules can raise **custom violations**, allowing a design-specific condition to surface through the same reporting path as a built-in rule. Violations of these rules are reported by the same DRC run, so a build gated on the report is gated on custom rules as well.

## The API transition: kicad-python (kipy)

Interactive work — placing a repeated connector footprint, bulk-editing net classes, reading pad positions — needs an object model rather than an exporter. Historically that meant `import pcbnew`, a Simplified Wrapper and Interface Generator (SWIG) binding to KiCad's C++ classes, executed inside KiCad's own embedded Python interpreter. That arrangement couples the script to the internal C++ layout: the exposed surface is whatever the C++ headers happen to declare, so it shifts as the internals shift.

KiCad 9 introduces the **IPC API**. The script runs as an ordinary external process and communicates with a *running* KiCad instance over a local socket, exchanging messages encoded with Protocol Buffers. The interface is therefore **the message schema, not the C++ class layout** — the boundary is explicit and versioned rather than implied by the binary. The client library is `kicad-python`, imported as `kipy`:

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

Two consequences follow from the client/server split. First, `KiCad()` fails if no editor is running, and `get_board()` returns the board **currently open in that editor** — the script does not name a file, so which design it edits is ambient state, not an argument. Second, edits land in the editor's in-memory document; the file on disk is unchanged until the document is saved.

In 9.0 the IPC API covers board editing, and the `pcbnew` SWIG bindings continue to work. The developer documentation describes the SWIG bindings as deprecated in favour of the IPC API, which makes `kipy` the target for new automation while leaving existing plugins functional for now.

## Application to a small ESP32 board

A typical Internet-of-Things (IoT) node repeats a handful of footprints — decoupling capacitors, headers, mounting holes — and the repetition is where manual editing introduces inconsistency. KiCad 9's **multichannel design** feature repeats a laid-out block within the GUI, covering the case where the same sub-circuit occurs several times. A `kipy` script covers the residual bulk edits that are not a block repetition. The `kicad-cli` export-and-DRC pair then runs unattended, so a board revision that fails DRC produces no fabrication package at all.

## Pitfalls

- **A `kipy` script silently edits the wrong board.** `get_board()` takes no path argument and returns whatever design the running editor has open; a second project opened in the same session redirects every subsequent call.
- **Changes made over IPC are not on disk.** The script mutates the editor's in-memory document, so a CI step that reads `board.kicad_pcb` afterwards sees the pre-edit file unless the document was saved.
- **A CI job invoking `kipy` hangs or fails at connect.** The IPC API requires a running KiCad instance; a headless build agent has none, and only `kicad-cli` runs without an editor.
- **A DRC gate passes while reporting violations.** `kicad-cli pcb drc` writes the report to `drc.json` and completes normally; a build step that never inspects the report's contents treats the file's existence as success.
- **Omitting `--severity-all` narrows the check.** The flag selects every severity class, so a report generated without it can be clean while lower-severity violations remain in the board.
- **Gerber output differs between runs despite an unchanged board.** The layer set is pinned to the repository only when `--layers` is passed explicitly; otherwise it is not part of the command under version control.
- **Automation written against `pcbnew` targets a deprecated interface.** The SWIG bindings still function in 9.0, but the developer documentation marks them deprecated in favour of the IPC API, so scripts bound to the C++ classes carry a migration cost.
