---
title: "Drawings that redraw themselves: scripting FreeCAD TechDraw from the Python console"
date: 2026-07-30
track: cad-3dprint
summary: "A parametric 3D model is only half the deliverable — someone still needs a dimensioned 2D print. FreeCAD's TechDraw workbench builds those drawing pages, and its Python API lets you regenerate the whole sheet when the model changes. Here's a macro that creates a page, projects front/top/iso views, dimensions them, adds a section, and exports PDF/SVG/DXF."
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

The parametric-model articles here (spreadsheet-driven dimensions, Python-generated geometry) all stop at the 3D solid. But a machinist, a laser-cutter shop, or a reviewer wants a *dimensioned 2D drawing* — front/top/iso views, real numbers, a title block, on a sheet. In FreeCAD that's the **TechDraw** workbench, and its Python API is what makes drawings first-class members of a parametric pipeline: when the model changes, a recompute redraws every view and every dimension, and one export call re-stamps the PDF. This is the piece OpenSCAD and most code-CAD tools don't give you.

I'm on FreeCAD 1.1 (released March 2026, currently 1.1.2); everything below also works on 1.0 (November 2024) with one path change noted inline. TechDraw's object model has been stable across both.

## The object model

A TechDraw drawing is a small tree of document objects, all created with `addObject`:

- `TechDraw::DrawPage` — the sheet.
- `TechDraw::DrawSVGTemplate` — the border/title-block SVG assigned to the page.
- `TechDraw::DrawViewPart` — a 2D projection of a Body/Part from a given direction.
- `TechDraw::DrawProjGroup` — a linked set of orthographic views (front + top + right…) that share a scale.
- `TechDraw::DrawViewSection` — a cut view derived from a base view.
- `TechDraw::DrawViewDimension` — a measurement attached to edges/vertices of a view.

Every one of these is a normal parametric object with a `Source` link back to your model, so a `doc.recompute()` propagates changes through all of them.

## One macro, whole sheet

Open the Python console (**View → Panels → Python console**) or the macro editor with a Body named `Body` in the active document, and run:

```python
import FreeCAD as App
import TechDraw, TechDrawGui

doc  = App.ActiveDocument
body = doc.getObject("Body")          # your PartDesign Body or Part::Feature

# 1. Page + template ----------------------------------------------------
page = doc.addObject("TechDraw::DrawPage", "Page")
tmpl = doc.addObject("TechDraw::DrawSVGTemplate", "Template")
tmpl.Template = App.getResourceDir() + \
    "Mod/TechDraw/Templates/Default_Template_A4_Landscape.svg"
#   FreeCAD 1.0: use ".../Mod/TechDraw/Templates/A4_Landscape_blank.svg"
page.Template = tmpl

# 2. Orthographic projection group (front is the anchor) ----------------
grp = doc.addObject("TechDraw::DrawProjGroup", "Views")
page.addView(grp)
grp.Source = [body]
grp.ProjectionType = "Third Angle"    # or "First Angle"
grp.ScaleType = "Custom"
grp.Scale = 1.0
front = grp.addProjection("Front")
grp.Anchor.Direction = (0, -1, 0)     # camera looks along -Y at the front
grp.Anchor.RotationVector = (1, 0, 0)
grp.addProjection("Top")
grp.addProjection("Right")
grp.X = page.PageWidth  * 0.40        # place the group on the sheet
grp.Y = page.PageHeight * 0.55

# 3. A standalone isometric ---------------------------------------------
iso = doc.addObject("TechDraw::DrawViewPart", "Iso")
page.addView(iso)
iso.Source = [body]
iso.Direction = (1, -1, 1)            # isometric eye direction
iso.ScaleType = "Custom"
iso.Scale = 0.8
iso.X, iso.Y = page.PageWidth * 0.82, page.PageHeight * 0.62

doc.recompute()
```

`ProjGroup.addProjection` returns the individual `DrawViewPart` for that direction — keep the `front` handle, because dimensions reference it. `ScaleType = "Custom"` pins the scale to your `Scale` value; leave it `"Automatic"` and TechDraw sizes views to fit the page instead.

## Dimensions

A dimension links to sub-elements of a *view* (not the 3D model) via `References2D`, a list of `(view, "SubName")` tuples. `Type` picks the flavour — `"DistanceX"`, `"DistanceY"`, `"Distance"`, `"Radius"`, `"Diameter"`, `"Angle"`:

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

Edge and vertex names (`Edge1`, `Vertex3`, …) come from the *projected* geometry, so the fastest way to find the right one is to hover over the view in the GUI once and read the status bar, then hard-code it. Because the reference is stored against the view, the dimension re-solves to the same edge after a model change and its value updates automatically.

## Section views

A section is a `DrawViewSection` bound to a base view, cutting along a plane you define with `SectionNormal` (and optionally `SectionOrigin`, which defaults to the shape's centre):

```python
sec = doc.addObject("TechDraw::DrawViewSection", "SectionA")
page.addView(sec)
sec.Source     = [body]
sec.BaseView   = front
sec.SectionNormal = (0, 0, 1)         # cut plane normal, in base-view space
sec.Scale      = 1.0
sec.X, sec.Y   = page.PageWidth * 0.40, page.PageHeight * 0.22
doc.recompute()
```

## Exporting

TechDraw exports without opening a dialog, which is what makes it scriptable in a headless pipeline. PDF and SVG go through `TechDrawGui`; DXF (2D outlines for a CAM or laser workflow) through `TechDraw`:

```python
TechDrawGui.exportPageAsPdf(page, "/tmp/bracket.pdf")
TechDrawGui.exportPageAsSvg(page, "/tmp/bracket.svg")
TechDraw.writeDXFPage(page, "/tmp/bracket.dxf")   # or writeDXFView(front, ...)
```

## The parametric payoff

Here's why this beats drawing by hand. Suppose the model's dimensions are driven from a spreadsheet (the alias-and-expression workflow covered earlier). A revised part just means changing a cell and recomputing — and the drawing follows:

```python
doc.getObject("Spreadsheet").set("board_len", "60 mm")
doc.recompute()                       # 3D updates → views + dims re-solve
TechDrawGui.exportPageAsPdf(page, "/tmp/bracket_rev2.pdf")
```

No view got moved, no dimension got re-attached, no number got retyped. The three-line block above *is* the release process for a new revision. Wrap the whole thing in a macro, and "regenerate all drawings" becomes a single click — the drawing sheet stops being a document you maintain and becomes an output you compute, exactly like the STL.

**Try next:** turn the snippet above into a saved macro that takes the Body and an output directory as arguments, then bind it to a toolbar button; change one spreadsheet cell, run the macro, and confirm the PDF, SVG, and DXF all regenerate with corrected dimensions — then deliberately delete an edge feature so `Edge1` no longer exists and watch which dimension goes red, which is TechDraw telling you a reference broke rather than silently printing a wrong number.
