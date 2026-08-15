---
title: "Edge6 is an index, not an identity: FreeCAD 1.0 and the topological naming problem"
date: 2026-07-30
track: cad-3dprint
summary: "For years FreeCAD's sharpest failure mode was a fillet or a sketch silently binding to the wrong edge after an upstream edit, because names such as Edge6 are reused list positions with no memory. FreeCAD 1.0 merged the realthunder element-mapping mitigation into mainline, but robust models still depend on what a feature attaches to. This article states what the topological naming problem is, what 1.0 changed, and which datum-first modelling habits keep a parametric tree from collapsing."
reading_time: 6
tags: [freecad, toponaming, parametric, partdesign, datum, cad]
sources:
  - title: "Topological naming problem — FreeCAD Documentation"
    url: "https://github.com/FreeCAD/FreeCAD-documentation/blob/main/wiki/Topological_naming_problem.md"
  - title: "FreeCAD's topological naming problem is (officially) history — Ondsel blog"
    url: "https://www.ondsel.com/blog/toponaming-problem-is-history/"
  - title: "Topological Naming — realthunder/FreeCAD_assembly3 Wiki"
    url: "https://github.com/realthunder/FreeCAD_assembly3/wiki/Topological-Naming"
  - title: "FreeCAD 1.0 launches with enhanced UI/UX, built-in Assembly Workbench, and TNP fixes — AlternativeTo"
    url: "https://alternativeto.net/news/2024/11/freecad-1-0-launches-with-enhanced-ui-ux-built-in-assembly-workbench-and-tnp-fixes/"
---

**Gist.** A downstream FreeCAD feature stores its reference to geometry as a string such as `Edge6`, but that string is a position in a list the geometry kernel regenerates on every recompute, so an upstream edit can silently rebind a fillet or a sketch to different geometry. FreeCAD 1.0, released **19 November 2024**, ships an element-mapping layer that gives each subelement a persistent mapped name derived from operation history, so broken references can be detected, suggested and in some cases repaired. The cost is that mapping is a **mitigation and not immunity**: the documentation continues to recommend datums and explicit placement, so models still carry the discipline the mapping was meant to relieve.

## The mechanism of the failure

Every solid in FreeCAD is a boundary representation (B-rep) composed of faces, edges and vertices. The geometry kernel beneath it, Open CASCADE Technology (OCCT), does not assign those subelements stable identities. FreeCAD historically named them with a **type-plus-index scheme** — `Face1`, `Edge6`, `Vertex3` — where the index is the position in the list the kernel returns.

The index carries no memory of what it denoted before. When upstream geometry changes — a sketch is edited, a pad added, a chamfer resized — OCCT rebuilds the shape and re-enumerates the list. The realthunder assembly3 wiki states the consequence directly: "the element indices are rearranged/reused", and "there is no easy way of tracking which one is which after the modification." An edge previously named `Edge6` may become `Edge9`, while a different edge takes the name `Edge6`.

Downstream features hold only the string. A fillet stores "apply a 2 mm round to `Edge6`"; a sketch stores "attach to `Face13`". After a recompute those strings still resolve to *an* edge and *a* face, and this is precisely the problem: **resolution succeeds even when it is wrong**. Two outcomes follow. Either the feature applies to different geometry than intended, which is silent and produces a wrong part, or the reference fails to resolve and the tree turns red, which is disruptive but honest. A representative case is the top face of a pad shifting from `Face13` to `Face14` after an upstream edit; a sketch attached to `Face13` is then orphaned or attached to a different plane.

This persisted across years of FreeCAD releases because it is not a defect in a single feature. **The data model had no persistent identity to reference in the first place**, so no downstream feature could store one.

## What FreeCAD 1.0 changed

The mitigation originated in the branch maintained by the developer known as **realthunder**, which introduced an element-mapping framework. Rather than storing only the enumerated name, the shape carries a **mapped name that travels with the subelement across operations**, derived from the operation history. The mapped name encodes the chain of operations and the source shapes an element descends from, so that elements arriving from different inputs of a boolean or compound do not collide.

Moving this from a fork into mainline was a multi-year effort by several contributors rather than a single patch. Ondsel's write-up marking the problem "officially history" describes the mitigation being enabled by default in development builds ahead of the 1.0 release.

Three capabilities follow from the mapping:

| Capability | Behaviour |
|---|---|
| Detection | Flags a broken subelement reference and surfaces the error, instead of binding silently to different geometry |
| Suggestion | Proposes the most likely correct element for confirmation |
| Repair | Re-binds the reference to the confirmed element, so the feature survives instead of being deleted and rebuilt |

The FreeCAD documentation states the limit plainly: this is a **mitigation, not immunity**. References are far more likely to survive edits, and the documentation still recommends datums and explicit placement. The mapping and the modelling habits are complementary rather than alternatives.

## Modelling habits that survive edits

The invariant behind every robust model is that **a feature should reference an entity whose identity the modeller defines, not one the kernel enumerates**. A datum plane exists in the model's own coordinate system and is never re-indexed. `Edge6` is a list position.

**1. Anchor sketches to datums rather than to solid faces.** Instead of selecting a solid face to begin a sketch, create a datum plane and attach the sketch to it. A datum-anchored workflow for a boss on the top of a part:

1. In the PartDesign Body, add a **Datum Plane**.
2. Attach it to a body origin plane such as `XY_Plane`, or to an existing sketch's own plane with the `ObjectXY` mode — neither is a kernel-enumerated subelement — then give it an explicit Z offset equal to the pad height, driven by a spreadsheet cell or an expression such as `Pad.Length`.
3. Sketch the boss on the datum plane and pad it.
4. Changing the base pad height then updates the datum offset, because the offset is an *expression*; the boss follows. No `Face13` was referenced, so no re-indexing can affect the result.

The distinction is that a datum plane's position is defined by declared parameters and therefore moves predictably, whereas a face's identity is defined by kernel enumeration and can change under any upstream edit.

**2. Base the first sketch on origin planes or body-level datums.** The `XY`, `XZ` and `YZ` origin planes of a Body are never re-indexed.

**3. Keep fillets and chamfers late and grouped.** Dress-up features consume the most fragile references — edges. Placing them as a final stage, after structural geometry is settled, limits the exposure of an early edit that churns edge enumeration. A single late fillet feature is less exposed than five scattered ones, even with 1.0's mapped names binding the selection.

**4. Use datum lines and points, and named constraints, for downstream references** in place of raw vertices picked off a solid.

**5. Treat the suggestion dialog as the mitigation working.** In 1.0 a broken reference is detected; when a replacement element is proposed, confirming it preserves the feature rather than requiring deletion and rebuild.

### Inspecting element names from the Python console

The mapped names are visible directly. With a PartDesign feature selected, open **View → Panels → Python console** and inspect its shape:

```python
obj = App.ActiveDocument.getObject("Pad")   # name of the feature under inspection
shp = obj.Shape

# Enumerated names — the unstable ones
for i, f in enumerate(shp.Faces, start=1):
    print("Face%d" % i, f.CenterOfMass)

# The element map, where the build exposes it: enumerated name -> mapped name
for name, mapped in getattr(shp, "ElementMap", {}).items():
    print(name, "->", mapped)

# Resolve a specific subelement the way a downstream feature would
print(shp.getElement("Face6"))
```

Editing an upstream sketch, recomputing and re-running the loops shows the effect: the `FaceN` index of a given face shifts, while the `ElementMap` entry is what allows FreeCAD 1.0 to follow that face across the change. A datum plane appears in neither listing, which is the reason it is the durable attachment target.

## Pitfalls

- **A recompute that produces no error is not evidence the references held.** Enumerated names resolve to whatever currently occupies the index, so a wrong binding completes silently and the part is wrong without a red tree.
- **A fillet added early consumes edges that later structural features re-enumerate.** The symptom is a fillet migrating to an interior edge after an unrelated upstream dimension change.
- **A sketch attached to a solid face inherits that face's identity risk.** When the face is renamed by an upstream edit, the sketch is orphaned or lands on a different plane, and every feature built on it moves with it.
- **A datum plane attached with a fixed numeric offset does not track the geometry it was meant to sit above.** Only an expression such as `Pad.Length` keeps the offset consistent when the pad height changes.
- **Deleting and rebuilding a feature discards the suggestion the mitigation offered.** The detection and suggestion path exists to re-bind the existing reference; rebuilding forfeits it and creates a fresh reference with the same exposure.
