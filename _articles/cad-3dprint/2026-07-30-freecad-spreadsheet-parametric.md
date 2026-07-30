---
title: "One spreadsheet, many parts: driving FreeCAD dimensions from a table"
date: 2026-07-30
track: cad-3dprint
summary: "Hard-coding dimensions into sketches means editing geometry every time a design changes. FreeCAD's Spreadsheet workbench lets you put your key dimensions in named cells and reference them from every constraint via expressions — so an enclosure resizes to a new board by editing two cells. Here's the alias-and-expression workflow."
reading_time: 5
tags: [freecad, parametric, spreadsheet, expressions, cad, enclosure]
sources:
  - title: "Spreadsheet Workbench — FreeCAD Documentation"
    url: "https://wiki.freecad.org/Spreadsheet_Workbench"
  - title: "Manual: Using spreadsheets — FreeCAD Documentation"
    url: "https://wiki.freecad.org/Manual:Using_spreadsheets"
  - title: "Expressions — FreeCAD Documentation"
    url: "https://wiki.freecad.org/Expressions"
  - title: "A pragmatic introduction to FreeCAD – part 9: expressions and configurations — silica.io"
    url: "https://www.silica.io/a-pragmatic-introduction-to-freecad-part-9-expressions-and-configurations/"
---

Every parametric CAD project eventually hits the same wall: your key dimensions are scattered across a dozen sketch constraints, and changing the board you're enclosing means hunting down each one. FreeCAD 1.0's Spreadsheet workbench solves this cleanly — put your dimensions in one table, give the cells names, and reference those names from every constraint. Change the table, the whole model regenerates. It's the same "single source of truth" idea as build123d or OpenSCAD (covered earlier here), but for people who'd rather stay in the GUI and keep the parameters in front of them.

## Step 1: build the parameter table

Switch to the **Spreadsheet** workbench and create a spreadsheet. Put your driving dimensions in cells — say a PCB you want to enclose:

| Cell | Value | Meaning |
|---|---|---|
| B1 | 55 | board length (mm) |
| B2 | 28 | board width (mm) |
| B3 | 1.6 | board thickness (mm) |
| B4 | 2.0 | wall thickness (mm) |
| B5 | 3.0 | standoff height (mm) |

The trick that makes cells usable in expressions is the **alias**. Right-click a cell → *Alias*, and give `B1` the alias `board_len`, `B2` → `board_width`, `B4` → `wall`, and so on. Now the cell has a *name* you can reference from anywhere in the document, instead of a fragile "cell B1" address. You can even attach units — type `55 mm` and FreeCAD treats it as a length quantity, so your expressions stay dimensionally correct.

## Step 2: reference the aliases from constraints

Anywhere FreeCAD accepts a number, you can instead click the small blue **f(x)** expression icon and type a formula referencing the spreadsheet. To make the enclosure's inner cavity match the board plus clearance:

```
# In a sketch constraint for the cavity length:
Spreadsheet.board_len + 1 mm

# Cavity width:
Spreadsheet.board_width + 1 mm

# Outer shell length = cavity + two walls:
Spreadsheet.board_len + 1 mm + 2 * Spreadsheet.wall

# Pad (extrude) length for the shell:
Spreadsheet.standoff + Spreadsheet.board_thickness + 5 mm
```

`Spreadsheet` is the object's name; `.board_len` is the alias. The expression engine understands units and arithmetic, so `2 * Spreadsheet.wall` resolves to `4 mm` and the constraint updates live. A constraint driven by an expression shows a small f(x) marker and can't be dragged by hand — which is exactly what you want, because its value now *belongs* to the table.

## Step 3: change two cells, regenerate everything

This is the payoff. A new revision of the board arrives at 60 × 30 mm. You edit `B1` and `B2`, hit recompute, and every cavity dimension, every wall, and the extrude depth all update together — because they're all derived from those two cells. No sketch archaeology, no forgetting one constraint and shipping a lid that doesn't close. The spreadsheet becomes the design's control panel.

A few habits that keep this maintainable:

- **Alias everything you'll reference; label the row next to it.** Put a human-readable description in the adjacent column (A) so future-you knows `B4` is wall thickness without opening a sketch.
- **Derive, don't duplicate.** If two dimensions must stay related (outer = inner + 2×wall), express one *in terms of* the other in the sheet or the constraint — never type the derived number twice, or they'll drift.
- **Read values back for a BOM.** Spreadsheet cells can also *compute* — a cell can hold `=board_len * board_width` to show the footprint area, or reference model measurements, so the same sheet doubles as a lightweight parts/dimensions summary.

## Where the spreadsheet ends and code begins

The Spreadsheet workbench is the sweet spot for a *fixed* set of named parameters you tune by hand — enclosures, brackets, plates that come in a few sizes. When you need *generated* geometry (a bolt pattern with N holes computed from a diameter, or a family of parts stamped out programmatically), that's where you graduate to FreeCAD's Python console or a code-CAD tool like build123d. Many projects run both: a spreadsheet for the headline dimensions a human sets, and a macro for the repetitive geometry those dimensions imply.

**Try next:** Model a simple two-part snap enclosure with every dimension driven from a five-cell spreadsheet, then change only `board_len` and `board_width` to a completely different board's size and confirm the lid, walls, and standoffs all regenerate to fit — then deliberately hard-code one wall thickness as a literal in a sketch and watch it become the one dimension that *doesn't* follow, which is the bug this whole workflow exists to prevent.
