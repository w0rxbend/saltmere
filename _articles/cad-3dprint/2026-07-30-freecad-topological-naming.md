---
title: "Edge6 doesn't mean what you think: FreeCAD 1.0 and the Topological Naming Problem"
date: 2026-07-30
track: cad-3dprint
summary: "For years FreeCAD's biggest footgun was a fillet or a sketch silently jumping to the wrong edge after you tweaked something upstream, because names like Edge6 are just reused indices with no memory. FreeCAD 1.0 merged the Realthunder element-mapping mitigation into mainline, but robust models still come down to what you attach to. Here's what TNP actually is, what 1.0 fixed, and the datum-first modeling habits that keep a parametric tree from imploding."
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

You padded a sketch, added a 2 mm fillet on the top-front edge, then went back and made the pad a little taller. FreeCAD recomputes, and the fillet is now wrapped around some random interior edge — or the whole tree has gone red. Nothing you did was wrong. You hit the **Topological Naming Problem (TNP)**, the single most notorious source of "why did my model explode?" in FreeCAD's history. FreeCAD 1.0, released **19 November 2024**, is the first mainline version to ship a real mitigation. This article is about what TNP is, what 1.0 actually changed, and — more importantly — how to model so it can't hurt you.

## What TNP actually is

Every solid in FreeCAD is a boundary representation (B-rep) made of faces, edges, and vertices. The geometry kernel underneath, OpenCASCADE (OCCT), doesn't give those subelements stable identities. FreeCAD historically named them with a plain **type + index** scheme: `Face1`, `Edge6`, `Vertex3`. The index is essentially the position in a list the kernel happens to hand back.

The problem is right there in the naming: the index has no memory. When you change upstream geometry — edit a sketch, add a pad, change a chamfer — OCCT rebuilds the shape and re-enumerates the list. As the realthunder assembly3 wiki puts it, "the element indices are rearranged/reused" and "there is no easy way of tracking which one is which after the modification." The edge that was `Edge6` might now be `Edge9`, and something else is now `Edge6`.

Downstream features don't know this. A fillet stores "put a 2 mm round on `Edge6`." A sketch stores "attach to `Face13`." After a recompute those strings still resolve to *an* edge and *a* face — just the wrong ones. So the feature either applies to the wrong geometry (silent and dangerous) or fails to resolve and paints your tree red (loud, but at least honest). The top face of a pad getting renamed `Face13` → `Face14` after an upstream edit is the canonical example: any sketch mapped to `Face13` is now orphaned or misaligned.

This plagued FreeCAD for years because it's not a bug you can just patch — it's a missing capability in the data model. There was no persistent identity to reference in the first place.

## What FreeCAD 1.0 changed

The fix originated in the **LinkStage / LinkDaily fork** maintained by the developer known as **realthunder**, who built an element-mapping framework that assigns each subelement a *stable mapped name* that travels with it across operations. Rather than only storing `Face1`, the shape carries a persistent identity derived from the operation history. The mapped names encode where an element came from — you'll see strings like `Edge10;:T1:6`, which records that the edge descends from a source shape tagged `1`. Mapping is preserved through compound operations by appending source tags so identities don't collide.

Getting this from a personal fork into mainline was a multi-year effort by both volunteers and paid developers (realthunder, Chris Hennes, bgbsww, CalligaroV, John Dupuy, and others), funded in part by the FreeCAD Project Association. Ondsel's write-up marking it "officially history" describes the mitigation being enabled by default in weekly builds ahead of the 1.0 release, with — notably — negligible performance impact.

What you get in 1.0 is threefold:

| Capability | What it does |
|---|---|
| Detection | Flags broken subelement references and surfaces the error immediately, instead of silently binding to the wrong edge |
| Suggestion | Proposes the most likely correct element for you to confirm |
| Auto-repair | In high-confidence cases, re-binds the reference automatically on recompute |

The crucial caveat, stated plainly in the FreeCAD documentation: this is a **mitigation, not immunity**. It makes references far more likely to survive edits, but it does **not** make good modeling discipline obsolete. The docs explicitly still recommend datums and explicit placement. So the fix and the habits are complementary, not either/or.

## Modeling habits that survive edits

The through-line of every robust FreeCAD model is: **reference things that are stable, not things the kernel generates.** A datum plane you placed lives in the model's own coordinate system and never gets re-indexed. `Edge6` is a lottery ticket.

**1. Anchor sketches to datums, not faces.** This is the highest-leverage habit. Instead of clicking a solid face to start a sketch, create a datum plane and attach the sketch to that.

A concrete datum-anchored workflow for, say, a boss on the top of a part:

1. In your PartDesign Body, before or after the base pad, add a **Datum Plane**.
2. Attach it with `FlatFace` to the base sketch's plane (a *sketch*, which is history-stable) — or to a body origin plane (`XY_Plane`) — then give it an explicit offset in Z equal to the pad height, driven by a spreadsheet cell or an expression like `Pad.Length`.
3. Sketch the boss on the datum plane. Pad it.
4. Now change the base pad height. The datum's offset is an *expression*, so it tracks the new height; the boss rides along. No `Face13` was ever referenced, so nothing can be re-indexed out from under you.

The key difference: a datum plane's position is defined by *your* parameters, so it moves predictably. A face's identity is defined by the kernel's enumeration, so it moves chaotically.

**2. Prefer origin planes and body-level datums for the first sketch.** Base every Body on its `XY`/`XZ`/`YZ` origin planes rather than importing or clicking geometry. These never re-index.

**3. Keep fillets and chamfers late and grouped.** Dress-up features consume the most fragile references (edges). Add them as a final stage, after the structural geometry is settled, so an early edit doesn't churn edges a fillet depends on. When you do select edges, letting 1.0's mapped names bind is far safer than in 0.21 — but a late, single fillet feature is still less exposed than five scattered ones.

**4. Use datum lines/points and named constraints for downstream references** instead of picking raw vertices off a solid.

**5. Watch for the red tree and trust the suggestion dialog.** In 1.0, a broken reference is now *detected*. When it offers a replacement element, that's the mitigation working — confirm it rather than deleting and rebuilding the feature.

### Inspecting element names from the Python console

You can see the mapped names directly. With a PartDesign feature selected, open **View → Panels → Python console** and inspect its shape:

```python
obj = App.ActiveDocument.getObject("Pad")   # or whatever your feature is named
shp = obj.Shape

# Old-style enumerated names — the unstable ones
for i, f in enumerate(shp.Faces, start=1):
    print("Face%d" % i, f.CenterOfMass)

# The 1.0 element map: enumerated name -> persistent mapped name
for name, mapped in shp.ElementMap.items():
    print(name, "->", mapped)

# Resolve a specific subelement the way a downstream feature would
print(shp.getElement("Face6"))
```

Edit an upstream sketch, recompute, and re-run the loop. Under the old scheme the `FaceN` you cared about shifts index; the `ElementMap` entry is what lets FreeCAD 1.0 follow it across the change. Seeing that mapping print out is the clearest way to internalize why referencing a datum — which never appears in this churn at all — is the durable choice.

## The takeaway

FreeCAD 1.0 turned TNP from a silent, model-destroying trap into a detected, usually-repairable event, by giving subelements the persistent identity they never had. That is a genuine milestone after years of the problem defining the tool's reputation. But the documentation is deliberate in saying it doesn't retire good habits: the models that never break are the ones built on datums, origin planes, and expressions — references that mean the same thing before and after every edit.

**Try next:** Take an existing part where a sketch is attached to a solid face, add a Datum Plane offset from an origin plane by an expression, re-attach the sketch to the datum, then change an upstream dimension and confirm nothing turns red.
