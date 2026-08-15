---
title: "KiCad 9's IPC API: Automating the Board Editor with kipy"
date: 2026-08-10
track: cad-3dprint
summary: KiCad 9 ships an out-of-process inter-process communication (IPC) API driven by the kicad-python (kipy) package. This article covers what replaced the in-process SWIG bindings, how a client connects to a running KiCad over a socket, and a script that lists footprints and pushes a change as a single undo step.
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

**Gist.** Scripting KiCad's PCB editor historically meant `import pcbnew`, a SWIG wrapper generated from KiCad's C++ classes, which placed the script inside KiCad's address space and bound it to internal C++ structure that changed across major releases. KiCad 9 replaces that model with an **inter-process communication (IPC) API**: the script runs in its own process and exchanges Protocol Buffers messages with a running KiCad over an NNG (nanomsg-next-generation) socket, with the official Python binding published as `kicad-python` and imported as `kipy`. The cost of the decoupling is that every board object is now a message copy rather than a live C++ pointer, edits are only visible to the editor once a commit is pushed, and no client can connect until the API server is enabled in KiCad's preferences.

## Out-of-process instead of SWIG

The SWIG `pcbnew` module linked the Python interpreter directly into KiCad's process, exposing `BOARD`, `FOOTPRINT` and related C++ classes. Three consequences followed from that arrangement: **plugin stability depended on KiCad's internal C++ layout**, an upstream refactor could break plugins without a deprecation window, and **a fault in a plugin ran in the editor's own address space**.

The IPC API inverts the direction of the coupling. The script talks to a **running KiCad instance over a socket**. Per the KiCad developer documentation, the transport is an [NNG](https://dev-docs.kicad.org/en/apis-and-binding/ipc-api/index.html) socket, and messages are serialised with **Protocol Buffers**. Two properties follow:

1. **A versioned contract.** The API follows protobuf practice: new KiCad versions may add messages and fields without changing the meaning of existing ones, so a message a client does not recognise does not invalidate the fields it does.
2. **Independence from the C++ internals.** Because the client exchanges structured messages rather than dereferencing objects, internal refactors do not by themselves alter the wire contract.

The interface is language-agnostic — any client able to speak Protocol Buffers over NNG can drive KiCad — and `kipy` is the binding KiCad publishes.

## Installing kipy and enabling the API server

The PyPI distribution is named `kicad-python`; the import name is `kipy`:

```bash
pip install kicad-python
```

The API server is **off by default**. It is enabled inside KiCad under **Preferences → Plugins → Enable the KiCad API**. Until that setting is on, nothing connects — neither a plugin launched from KiCad nor a script started from a terminal. When KiCad launches an API plugin it sets the `KICAD_API_SOCKET` and `KICAD_API_TOKEN` environment variables, so `kipy` connects without explicit configuration; for a standalone script run against an already-open KiCad, the default constructor locates the socket.

## Connecting and reading the board

The entry point is the `KiCad` object:

```python
from kipy import KiCad

kicad = KiCad()                     # discovers the running instance's socket
print(kicad.get_version())          # connection check: the connected KiCad version

board = kicad.get_board()           # reference to the PCB open in the editor
```

`KiCad()` accepts optional arguments, among them `socket_path`, `client_name` and `timeout_ms`. Against an interactive session the zero-argument form suffices, and `kicad.ping()` tests the connection. **`get_board()` presupposes a board open in the editor**; a script run against a KiCad window with no PCB loaded has no board to work on, and the failure surfaces at the first call rather than at connection time.

The following script lists every footprint with its reference and position, then moves one component and commits the move as a single undo step:

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

    board = kicad.get_board()   # requires a .kicad_pcb open in the editor

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

Three properties of the object model carry the weight, all documented in the [`kipy` reference](https://docs.kicad.org/kicad-python-main/board.html):

- **Coordinates are integer nanometres.** `position` is a `Vector2` whose `.x` and `.y` are integers in nanometres; conversion to millimetres divides by 1,000,000. Arithmetic on board geometry therefore stays exact — no floating-point representation error accumulates across repeated edits.
- **Text is one level below the field.** A footprint's reference is not a string but a `Field`; the text is read through `reference_field.text.value`, and the value through `value_field.text.value`. This mirrors KiCad's internal model of editable text fields.
- **Edits are transactional.** `board.begin_commit()` returns a `Commit`. **Modifications are not reflected in the editor until `board.push_commit(commit, message)` is called**, at which point they land as a single undo entry carrying that message. `board.drop_commit(commit)` discards them. `update_items()` matches existing items **by UUID**, and the sibling methods `create_items()` and `remove_items()` add and delete.

The state machine per edit is therefore: begin → mutate local message copies → `update_items` to stage → `push_commit` to publish, or `drop_commit` to abandon. Omitting the final step leaves the mutation entirely in the client process; the board on screen is unchanged and no error is raised.

## Iterating tracks and nets

The same `Board` object exposes the remaining geometry. `board.get_tracks()` returns every `Track` and `ArcTrack`; `board.get_nets()` returns `Net` objects and optionally filters by net class:

```python
for track in board.get_tracks():
    print(f"net={track.net.name:12s} width={nm_to_mm(track.width):.3f} mm")

power = board.get_nets(netclass_filter="Power")
print(f"{len(power)} nets in the Power netclass")
```

`board.get_shapes()` covers graphic shapes. Modifications to any of these pass through the same commit machinery.

## Contrast with the deprecated SWIG pcbnew

| | SWIG `pcbnew` | IPC API (`kipy`) |
|---|---|---|
| Where it runs | Inside KiCad's process | Separate process, over a socket |
| Coupling | Directly to C++ internals | Versioned protobuf contract |
| Transport | In-memory objects | NNG + Protocol Buffers |
| Stability | Tied to C++ layout across majors | Additive, versioned messages |
| Undo | Manual | First-class commits |

## Current status

As of KiCad 9 the IPC API is shipping, with `kicad-python` as the supported binding. The API is a first cut: coverage of the board model is incomplete and the surface is still growing. The SWIG bindings remain available in KiCad 9 and are documented as deprecated, with removal announced for a later release; **no removal version is fixed in the sources cited here**, so the migration deadline for existing plugins is not yet a date.

## Pitfalls

- Scripts fail to connect with the API server left at its default: the setting under **Preferences → Plugins → Enable the KiCad API** is off until changed, and no amount of client-side configuration substitutes for it.
- A script run against a KiCad window with no PCB open fails at `kicad.get_board()`: connecting successfully says nothing about a board being loaded.
- Positions treated as millimetres are wrong by a factor of a million: `Vector2.x` and `.y` are integer nanometres.
- Reading `fp.reference_field` and expecting a string fails: the reference is a `Field`, and the text is at `reference_field.text.value`.
- Edits that are never pushed vanish silently — mutations without a matching `push_commit` alter only the client-side message copies, leaving the editor untouched and raising nothing.
- Long-running operations against a default client can time out: `KiCad()` applies a client-side `timeout_ms`, so a request the editor has not answered within that window fails in the script rather than in KiCad.
- New items created outside the commit machinery are not matched by `update_items()`, which resolves items by UUID; `create_items()` is the method for adding.
