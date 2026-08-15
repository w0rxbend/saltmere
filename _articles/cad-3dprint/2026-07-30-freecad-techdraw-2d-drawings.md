---
title: "Scripting FreeCAD TechDraw: 2D drawing pages as computed output"
date: 2026-07-30
track: cad-3dprint
summary: "A parametric 3D model is only half the deliverable; a machinist still needs a dimensioned 2D print. FreeCAD's TechDraw workbench builds those drawing pages as parametric document objects, and its Python API regenerates the whole sheet on recompute. A macro creates a page, projects front/top/iso views, dimensions them, adds a section, and exports PDF, SVG and DXF."
reading_time: 6
tags: [freecad, techdraw, 2d-drawing, python, macro, parametric, cad]
sources:
  - title: "FreeCAD Version 1.0 Released — FreeCAD News"
    url: "https://blog.freecad.org/2024/11/19/freecad-version-1-0-released/"
  - title: "TechDraw Workbench — FreeCAD Documentation"
    url: "https://wiki.freecad.org/TechDraw_Workbench"
  - title: "TechDraw API — FreeCAD Documentation"
    url: "https://wiki.freecad.org/TechDraw_API"
  - title: "TechDrawGui API — FreeCAD Documentation"
    url: "https://wiki.freecad.org/TechDrawGui_API"
  - title: "TechDraw ProjectionGroup — FreeCAD Documentation"
    url: "https://wiki.freecad.org/TechDraw_ProjectionGroup"
---

**Gist.** A parametric modelling pipeline that ends at the 3D solid still leaves the dimensioned 2D drawing — the artefact a machinist, a laser-cutter shop or a reviewer reads — as hand-maintained work that drifts out of step with the model. FreeCAD's TechDraw workbench represents the sheet, its views and its dimensions as ordinary parametric document objects linked back to the source shape, so a document recompute re-projects every view and re-solves every dimension, and a single call re-exports the file. The cost is that dimensions are anchored not to model features but to **named sub-elements of the projected 2D geometry** (`Edge1`, `Vertex3`), whose numbering is an output of the projection and can be reassigned when the model changes.

The material below targets the FreeCAD 1.x series, whose first release was 1.0 in November 2024. The object model and the API names used here are those documented on the TechDraw wiki pages; the one detail that has moved between releases is the file name of the stock page template, noted inline.

## The object model

A TechDraw drawing is a small tree of document objects, each created through `addObject` on the document:

- `TechDraw::DrawPage` — the sheet itself.
- `TechDraw::DrawSVGTemplate` — the border and title-block SVG assigned to the page.
- `TechDraw::DrawViewPart` — a 2D projection of a Body or Part from one direction.
- `TechDraw::DrawProjGroup` — a linked set of orthographic views (front, top, right, …) sharing one scale.
- `TechDraw::DrawViewSection` — a cut view derived from a base view.
- `TechDraw::DrawViewDimension` — a measurement attached to edges or vertices of a view.

The property that makes the pipeline work is that **each of these is a normal parametric object holding a `Source` link back to the model**, so `doc.recompute()` propagates a model change through the projection, then through the dimensions that reference the projection, in dependency order. Nothing in the sheet is a stored snapshot.

## Constructing a sheet

The following runs in the Python console (**View → Panels → Python console**) or the macro editor, with a Body named `Body` in the active document.

```python
import FreeCAD as App
import TechDraw, TechDrawGui

doc  = App.ActiveDocument
body = doc.getObject("Body")          # PartDesign Body or Part::Feature

# 1. Page + template ----------------------------------------------------
page = doc.addObject("TechDraw::DrawPage", "Page")
tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
tmpl.Template = App.getResourceDir() + \
    "Mod/TechDraw/Templates/A4_LandscapeTD.svg"
#   file names in this directory differ between releases; list it before assuming
page.Template = tmpl

# 2. Orthographic projection group (front is the anchor) ----------------
grp = doc.addObject("TechDraw::DrawProjGroup", "Views")
page.addView(grp)
grp.Source = [body]
grp.ProjectionType = "Third Angle"    # or "First Angle"
grp.ScaleType = "Custom"
grp.Scale = 1.0
front = grp.addProjection("Front")
grp.Anchor.Direction = (0, -1, 0)     # projection direction of the front view
grp.addProjection("Top")
grp.addProjection("Right")
grp.X = tmpl.Width  * 0.40            # placement on the sheet, in mm
grp.Y = tmpl.Height * 0.55

# 3. A standalone isometric ---------------------------------------------
iso = doc.addObject("TechDraw::DrawViewPart", "Iso")
page.addView(iso)
iso.Source = [body]
iso.Direction = (1, -1, 1)            # isometric eye direction
iso.ScaleType = "Custom"
iso.Scale = 0.8
iso.X, iso.Y = tmpl.Width * 0.82, tmpl.Height * 0.62

doc.recompute()
```

Two details carry the construction. `ProjGroup.addProjection` **returns the individual `DrawViewPart` for that direction**, and that returned handle is the object dimensions must reference; the group as a whole is not a dimensionable view. And `ScaleType = "Custom"` **pins the scale to the assigned `Scale` value**, whereas the `"Automatic"` setting sizes views to fit the page — meaning the drawn scale becomes a function of the model's bounding box and changes silently when the model does.

## Dimensions and their references

A dimension does not attach to the 3D model. It attaches to sub-elements of a *view*, through `References2D`, a list of `(view, "SubName")` tuples. `Type` selects the flavour: `"DistanceX"`, `"DistanceY"`, `"Distance"`, `"Radius"`, `"Diameter"`, `"Angle"`.

```python
w = doc.addObject("TechDraw::DrawViewDimension", "Width")
page.addView(w)
w.Type = "DistanceX"
w.References2D = [(front, "Edge1")]   # a horizontal edge of the front view
w.recomputeFeature()

h = doc.addObject("TechDraw::DrawViewDimension", "Height")
page.addView(h)
h.Type = "DistanceY"
h.References2D = [(front, "Edge2")]
doc.recompute()
```

The sub-element names (`Edge1`, `Vertex3`, …) belong to the **projected** geometry rather than to the solid, so they are not visible from the model tree. Hovering over the view in the graphical interface reports the name in the status bar, which is the practical way to discover the correct index before hard-coding it. Because the reference is stored against the view, a model change that leaves the same edge present re-solves the dimension and updates its value without further intervention.

## Section views

A `DrawViewSection` is bound to a base view and cuts along a plane defined by `SectionNormal`, expressed in base-view space. `SectionOrigin` is the point the cut plane passes through.

```python
sec = doc.addObject("TechDraw::DrawViewSection", "SectionA")
page.addView(sec)
sec.Source     = [body]
sec.BaseView   = front
sec.SectionNormal = (0, 0, 1)         # cut plane normal, in base-view space
sec.Scale      = 1.0
sec.X, sec.Y   = tmpl.Width * 0.40, tmpl.Height * 0.22
doc.recompute()
```

## Export

TechDraw's export calls take a path and return without opening a dialog, which is what permits their use in an unattended pipeline. PDF and SVG are issued through `TechDrawGui`; DXF — 2D outlines for a computer-aided-manufacturing or laser workflow — through `TechDraw`.

```python
TechDrawGui.exportPageAsPdf(page, "/tmp/bracket.pdf")
TechDrawGui.exportPageAsSvg(page, "/tmp/bracket.svg")
TechDraw.writeDXFPage(page, "/tmp/bracket.dxf")   # or writeDXFView(front, ...)
```

Note the module split: **the PDF and SVG exporters live in `TechDrawGui`**, so they depend on the graphical-interface module being importable, while `writeDXFPage` does not.

## The parametric consequence

Where the model's dimensions are driven from a spreadsheet through the alias-and-expression workflow, a revision reduces to changing a cell and recomputing.

```python
doc.getObject("Spreadsheet").set("board_len", "60 mm")
doc.recompute()                       # 3D updates → views + dims re-solve
TechDrawGui.exportPageAsPdf(page, "/tmp/bracket_rev2.pdf")
```

No view is repositioned, no dimension re-attached, no value retyped: the recompute walks the dependency graph from the spreadsheet cell to the solid, from the solid to each `DrawViewPart`, and from each view to the dimensions holding `References2D` against it. The three lines above constitute the revision procedure. Packaged as a macro, the drawing sheet ceases to be a maintained document and becomes a computed output on the same footing as the exported STL.

**Try next:** convert the snippet into a saved macro parameterised by the Body and an output directory, bind it to a toolbar button, change one spreadsheet cell, and confirm that PDF, SVG and DXF all regenerate with corrected values. Then delete an edge feature so that `Edge1` no longer exists, and observe which dimension is marked in error — the visible signal that a reference broke rather than a silently wrong printed number.

## Pitfalls

- **A dimension references projected geometry, not model features.** Adding a fillet or chamfer renumbers the projection's edge list, so a hard-coded `Edge1` can silently resolve to a different edge and print a plausible but wrong value.
- **Referencing the projection group instead of the returned view fails.** `DrawProjGroup` is a container; `References2D` must name the `DrawViewPart` handle returned by `addProjection`, not `grp`.
- **`ScaleType = "Automatic"` makes the printed scale a function of the bounding box.** A model change resizes views to fit the page, so a sheet believed to be 1:1 is issued at another scale without any edit to the drawing.
- **Stock template file names are not stable across releases.** Assigning a path that does not exist yields a page with no border or title block rather than an error, so the templates directory under `App.getResourceDir()` is worth listing before hard-coding a name.
- **`exportPageAsPdf` and `exportPageAsSvg` are in `TechDrawGui`.** A script that imports only `TechDraw` can write DXF but raises on the PDF and SVG calls.
- **Projection-type convention is per group.** `"Third Angle"` and `"First Angle"` place top and right views on opposite sides; a sheet issued under the wrong convention is geometrically correct and still misread by the shop.
