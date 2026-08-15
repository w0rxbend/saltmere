---
title: "One spreadsheet, many parts: driving FreeCAD dimensions from a table"
date: 2026-07-30
track: cad-3dprint
summary: "Hard-coding dimensions into sketches means editing geometry whenever a design changes. FreeCAD's Spreadsheet workbench holds the driving dimensions in named cells that every constraint references through expressions, so an enclosure resizes to a new board by editing two cells. The alias-and-expression workflow, and the failure modes it does not remove."
reading_time: 6
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

**Gist.** In a hand-built computer-aided design (CAD) model the driving dimensions are duplicated across a dozen independent sketch constraints, so a change of input requires locating and editing each copy, and any copy missed silently produces a part that no longer fits. FreeCAD's Spreadsheet workbench removes the duplication by holding each dimension once in a named cell and letting every constraint reference that name through an expression, making the dependency explicit to the recompute engine. The cost is that an expression-driven constraint is no longer a number a human can drag: its value belongs to the table, edits must go through the table, and a single literal left behind in a sketch becomes an invisible exception to the rule.

## The invariant the workflow establishes

The property being enforced is single definition: **each independent dimension is written exactly once in the document, and every other occurrence is a derivation of it**. A model satisfying that property has the useful consequence that the set of things a human may edit is exactly the set of aliased cells, and everything else is a computed function of them.

The property is not enforced by FreeCAD. Nothing prevents a literal `2.0` typed directly into a constraint. The workflow is a discipline; the spreadsheet supplies the mechanism that makes the discipline cheap enough to keep.

## Step 1: the parameter table

The Spreadsheet workbench creates a spreadsheet object inside the document. Driving dimensions go in cells — for an enclosure around a printed circuit board (PCB):

| Cell | Value | Meaning |
|---|---|---|
| B1 | 55 | board length (mm) |
| B2 | 28 | board width (mm) |
| B3 | 1.6 | board thickness (mm) |
| B4 | 2.0 | wall thickness (mm) |
| B5 | 3.0 | standoff height (mm) |

What makes a cell usable from the rest of the document is the **alias**, a name bound to a cell through the cell's properties dialog: `B1` to `board_len`, `B2` to `board_width`, `B3` to `board_thickness`, `B4` to `wall`, `B5` to `standoff`. **The alias is the referencing key: an expression names `board_len` rather than the address `B1`, and the FreeCAD documentation recommends the alias form for references from outside the sheet.**

A cell may also carry a unit. Entering `55 mm` stores a length quantity rather than a dimensionless number, and the expression engine then propagates the dimension through arithmetic. **Adding a quantity to a plain number is a unit mismatch the engine rejects, so the error surfaces at edit time rather than as a silently wrong dimension.** Multiplication is unaffected: a plain number scales a quantity and the unit carries through.

## Step 2: referencing aliases from constraints

Wherever FreeCAD accepts a numeric input, the **f(x)** icon opens the expression editor and replaces the number with an expression. For the enclosure's inner cavity:

```
# Sketch constraint, cavity length:
Spreadsheet.board_len + 1 mm

# Cavity width:
Spreadsheet.board_width + 1 mm

# Outer shell length = cavity + two walls:
Spreadsheet.board_len + 1 mm + 2 * Spreadsheet.wall

# Pad (extrude) length for the shell:
Spreadsheet.standoff + Spreadsheet.board_thickness + 5 mm
```

`Spreadsheet` is the object's name in the document tree; the suffix after the dot is the alias. `2 * Spreadsheet.wall` resolves to `4 mm`, carrying the unit through the multiplication.

A constraint bound to an expression is marked with the f(x) indicator and **cannot be dragged in the sketcher**. That restriction is the visible half of the invariant: the constraint's value is no longer stored in the sketch, so there is nothing local to drag. The value is recomputed from the spreadsheet whenever the document recomputes.

## Step 3: the recompute

Changing `B1` and `B2` to a 60 × 30 mm board and recomputing propagates through every constraint that references those aliases: cavity length and width, outer shell length, and any extrude depth derived from them. **The set of updated features is precisely the set that declared a dependency by naming an alias — nothing more and nothing less.** A wall thickness typed as a literal is outside that set and does not move, which is the failure this workflow exists to make impossible, and the failure that returns the moment one literal creeps back in.

Practices that keep the property intact:

- **Alias every cell that is referenced, and label it in the adjacent column.** Column A holding "wall thickness (mm)" next to `B4` keeps the sheet readable without opening a sketch to infer what the number drives.
- **Derive rather than duplicate.** Where two dimensions must stay related — outer equals inner plus twice the wall — one is expressed in terms of the other, in the sheet or in the constraint. A derived number typed twice has two independent definitions and drifts on the first edit that touches only one.
- **Compute inside the sheet where useful.** Cells can hold formulas as well as constants, so a cell holding a product of two aliased dimensions reports a footprint area, and the same sheet doubles as a dimensional summary of the part.

## Where the spreadsheet stops

The Spreadsheet workbench addresses a **fixed** set of named parameters adjusted by hand: enclosures, brackets, plates offered in a few sizes. The table has one row per parameter and the model has one expression per constrained dimension, so the structure of the model is static and only the numbers move.

Geometry whose *structure* depends on a parameter — a bolt circle whose hole count is computed from a diameter, or a family of parts generated in bulk — is not expressible this way, because the number of features is itself a variable. That work belongs to FreeCAD's Python console or to a code-CAD system such as build123d. The two combine: the spreadsheet holds the headline dimensions a human sets, and a macro generates the repetitive geometry those dimensions imply.

## Pitfalls

- **A dimension typed as a literal in one sketch stays fixed through every recompute.** Its symptom is a part that resizes almost correctly — a lid that no longer meets the walls — and the cause is that the constraint declared no dependency on any alias, so the recompute engine had no reason to visit it.
- **A cell referenced by address carries no indication of what it holds.** The symptom is a reference that still resolves after the sheet is rearranged but no longer names the intended parameter; the cause is that the address encodes a position rather than an identity, which is why the documentation recommends aliases.
- **Adding a unitless cell to a quantity is rejected by the expression engine.** The symptom is an expression that refuses to commit; the cause is that `55` and `55 mm` are different types to the engine, and a sheet built inconsistently produces this only for the expressions that combine the two.
- **Deleting an alias orphans every expression that used it.** The dependency is by name, so each broken reference must be repaired individually.
- **An expression-bound constraint cannot be dragged in the sketcher.** Attempting to adjust geometry by hand appears as an unresponsive sketch; the cause is that the value is held in the spreadsheet, and the edit must be made there.
