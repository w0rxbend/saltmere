---
title: "KiCad 9's IPC API: Automating the Board Editor with kipy"
date: 2026-08-10
track: cad-3dprint
summary: KiCad 9 ships a new out-of-process IPC API driven by the kicad-python (kipy) package. Here's why it replaced the in-process SWIG bindings, how to connect to a running KiCad over a socket, and a runnable script that lists footprints and pushes a change as a single undo step.
reading_time: 6
tags:
  - kicad
  - pcb
  - python
  - automation
  - ipc-api
sources:
  - title: "kicad-python (kipy) — official API reference"
    url: https://docs.kicad.org/kicad-python-main/kicad.html
  - title: "Board — kicad-python documentation"
    url: https://docs.kicad.org/kicad-python-main/board.html
  - title: "KiCad Developer Docs — IPC API"
    url: https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html
  - title: "kicad-python on PyPI"
    url: https://pypi.org/project/kicad-python/
  - title: "KiCad 9.0 Python API (IPC API) — KiCad.info forum"
    url: https://forum.kicad.info/t/kicad-9-0-python-api-ipc-api/57236
---

For years, scripting KiCad's PCB editor meant `import pcbnew` — a SWIG wrapper generated directly from KiCad's C++ classes. It worked, but it was fragile in an annoying way: your script ran *inside* KiCad's process, bound tightly to internal C++ objects that the KiCad team could rename or restructure at any release. Every major version broke something. KiCad 9 changes the model entirely with a new **IPC API**, and the official Python binding for it is the `kicad-python` package, imported as `kipy`.

This post is a practical walk-through: why the architecture changed, how to install and connect, and a runnable script that iterates footprints and commits a change.

## Why out-of-process instead of SWIG

The old SWIG `pcbnew` module linked your Python interpreter straight into KiCad's address space. That gave you raw access to `BOARD`, `FOOTPRINT`, and friends — but it also meant:

- Your plugin's stability was hostage to KiCad's internal C++ layout. A refactor upstream broke plugins with no deprecation window.
- There was no real API contract. You were poking at implementation details, not a designed interface.
- A misbehaving plugin could take the whole editor down with it.

The IPC API inverts this. Instead of loading into KiCad, your script talks to a **running KiCad instance over a socket**. Per the KiCad developer docs, the transport is [NNG (nanomsg-next-gen)](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html) over a UNIX socket, and messages are serialized with **Protocol Buffers**. That choice buys two things that matter:

1. **A stable, versioned contract.** The API follows protobuf best practices: new KiCad versions may add messages and fields, but won't change the meaning of existing ones. Deprecated items are supported for at least one more major release.
2. **Decoupling from the C++ internals.** Because you exchange structured protobuf messages rather than dereferencing C++ objects, KiCad's team can refactor internals freely without breaking your automation.

It's also language-agnostic — anything that can speak protobuf over NNG can drive KiCad — but `kipy` is the blessed Python path.

## Installing kipy and enabling the API server

The package on PyPI is named `kicad-python` (current release **0.7.1**, April 2026, Python ≥3.9), but you import it as `kipy`:

```bash
pip install kicad-python
```

One gotcha that catches everyone: the API server is **off by default**. You must turn it on inside KiCad under **Preferences → Plugins → Enable the KiCad API**. Nothing — not a plugin launched from KiCad, not a script run from your own terminal — can talk to KiCad until that box is checked. When KiCad launches an API plugin it sets the `KICAD_API_SOCKET` and `KICAD_API_TOKEN` environment variables so `kipy` connects automatically; when you run a script standalone against an already-open KiCad, the default constructor finds the socket for you.

## Connecting and reading the board

The entry point is the `KiCad` object. With KiCad open and a board loaded:

```python
from kipy import KiCad

kicad = KiCad()                     # auto-discovers the running instance's socket
print(kicad.get_version())          # sanity check: prints the connected KiCad version

board = kicad.get_board()           # a reference to the PCB open in the editor
```

`KiCad()` accepts optional arguments — `socket_path`, `client_name`, `timeout_ms` (default 2000), and a `headless` mode that can spawn `kicad-cli` to work on a file without the GUI — but the zero-argument form is what you want against an interactive session. `kicad.ping()` tests the connection, and `kicad.get_board()` returns `None` if no board is open.

Here's a complete, runnable script that lists every footprint with its reference and position, then nudges one component and commits the move as a single undo step:

```python
#!/usr/bin/env python3
"""List footprints, then move C1 by 1 mm — KiCad 9 IPC API (kipy)."""
from kipy import KiCad
from kipy.geometry import Vector2

NM_PER_MM = 1_000_000  # KiCad's internal unit is the nanometre


def nm_to_mm(v: int) -> float:
    return v / NM_PER_MM


def main() -> None:
    kicad = KiCad()
    print(f"Connected to {kicad.get_version()}")

    board = kicad.get_board()
    if board is None:
        raise SystemExit("No board open in KiCad — open a .kicad_pcb first.")

    footprints = board.get_footprints()
    print(f"{len(footprints)} footprints on the board:\n")

    target = None
    for fp in footprints:
        ref = fp.reference_field.text.value    # e.g. "R1", "C1", "U3"
        val = fp.value_field.text.value        # e.g. "10k", "100nF"
        pos = fp.position                      # Vector2, in nanometres
        print(f"  {ref:6s} {val:10s} @ ({nm_to_mm(pos.x):8.3f}, "
              f"{nm_to_mm(pos.y):8.3f}) mm")
        if ref == "C1":
            target = fp

    if target is None:
        return

    # Modify a property, then commit it as one atomic undo step.
    commit = board.begin_commit()
    try:
        target.position = Vector2.from_xy(
            target.position.x + NM_PER_MM,     # +1 mm in X
            target.position.y,
        )
        board.update_items(target)             # match by internal UUID
        board.push_commit(commit, "Nudge C1 +1mm")
        print("\nMoved C1 by 1 mm and pushed the commit.")
    except Exception:
        board.drop_commit(commit)              # roll back on failure
        raise


if __name__ == "__main__":
    main()
```

A few things worth calling out, all verified against the [`kipy` reference](https://docs.kicad.org/kicad-python-main/board.html):

- **Coordinates are nanometres.** `position` is a `Vector2` whose `.x` / `.y` are integers in nm. Divide by 1,000,000 for millimetres. This is a deliberate integer-only design — no floating-point drift on your board geometry.
- **Text lives one level down.** A footprint's reference isn't a bare string; it's a `Field`, and you read the actual text via `reference_field.text.value` (and `value_field.text.value` for the value). That mirrors how KiCad models editable text fields internally.
- **Changes are transactional.** `board.begin_commit()` returns a `Commit` object. Your edits aren't reflected in the editor until you call `board.push_commit(commit, message)`, which lands them as a *single* undo entry with your message attached. If anything goes wrong, `board.drop_commit(commit)` discards them. `update_items()` matches existing items by UUID; there are sibling `create_items()` and `remove_items()` methods for adding and deleting.

## Iterating tracks and nets

The same `Board` object exposes the rest of the design. `board.get_tracks()` returns every `Track` and `ArcTrack`; `board.get_nets()` returns all `Net` objects and optionally filters by net class:

```python
for track in board.get_tracks():
    print(f"net={track.net.name:12s} width={nm_to_mm(track.width):.3f} mm")

power = board.get_nets(netclass_filter="Power")
print(f"{len(power)} nets in the Power netclass")
```

`board.get_shapes()` covers graphic shapes, and everything routes through the same commit machinery when you modify it.

## Contrast with the deprecated SWIG pcbnew

| | SWIG `pcbnew` | IPC API (`kipy`) |
|---|---|---|
| Where it runs | Inside KiCad's process | Separate process, over a socket |
| Coupling | Directly to C++ internals | Versioned protobuf contract |
| Transport | In-memory objects | NNG + Protocol Buffers |
| Stability | Broke across majors | Deprecation window, back-compat |
| Undo | Manual / awkward | First-class commits |

## Current status

As of KiCad 9, the IPC API is **stable and shipping** — `kicad-python` 0.7.x is the supported binding. It's still maturing (the KiCad team frames v9 as the developer-focused first cut, with ergonomics improving in v10), and the old SWIG bindings remain available in KiCad 9 and 10. But the direction is unambiguous: per the package's own notes, **SWIG is removed in KiCad 11**. If you're writing new automation, write it against `kipy`. Keep existing SWIG plugins running, but don't build anything new on a foundation with a known removal date.

**Try next:** Enable the API server, open one of your own boards, and run the script above unmodified to dump every footprint's reference and position. Then adapt the commit block to bulk-set the value field on a group of footprints — e.g. normalise every `100nF` cap to a canonical string — and watch it land as one undo step.
