---
title: "3MF vs STL: why 3D printing is quietly leaving triangle soup behind"
date: 2026-08-11
track: cad-3dprint
summary: "STL is a bag of unitless triangles with no units, colors, or metadata. 3MF is a zipped XML package with explicit units, an object/build hierarchy, materials, and extensions — and modern slicers use it to round-trip an entire print job in one file. Here's what's inside a .3mf and why the ecosystem is switching."
reading_time: 6
tags: [3mf, stl, 3d-printing, slicer, file-formats, cad]
sources:
  - title: "3MF Core Specification (3MF Consortium)"
    url: "https://github.com/3MFConsortium/spec_core/blob/master/3MF%20Core%20Specification.md"
  - title: "3MF Specification & Extensions index"
    url: "https://3mf.io/spec/"
  - title: "3MF Consortium releases the Beam Lattice extension"
    url: "https://www.tctmagazine.com/3mf-releases-beam-lattice-extension-3d-printing/"
  - title: "Saving projects as 3MF (Prusa Knowledge Base)"
    url: "https://help.prusa3d.com/article/saving-projects-as-3mf_1773"
  - title: "3D Manufacturing Format (Wikipedia)"
    url: "https://en.wikipedia.org/wiki/3D_Manufacturing_Format"
---

STL has been the lingua franca of desktop 3D printing since the 1980s, and it earned that spot by being almost aggressively simple. But "simple" here means *lossy*: an STL file is a flat list of triangles and nothing else. As parts get more complex — multiple objects on a plate, colors, per-object print settings, lattice infill — the format runs out of room, and the ecosystem has spent the last few years moving to 3MF to get that room back.

## What STL actually stores (and doesn't)

An STL triangle is three vertices and a face normal. That's the whole vocabulary. Concretely, the format has no concept of:

- **Units.** A coordinate of `10` might be 10 mm or 10 inches — the file never says. Every import is a guess, and cross-tool scaling bugs are a rite of passage.
- **Color or material.** No per-face color, no material assignment. Multi-material printers get nothing to work with.
- **Multiple objects.** One STL is one mesh. A plate of five parts is either five files or one merged blob you can't cleanly separate again.
- **Per-object settings, metadata, or provenance.** No author, no license, no "print this one at 0.2 mm."

It's also wasteful. STL stores each triangle as three *full* vertices, so a shared corner between two triangles is written twice with no indexing. The binary variant even keeps a per-triangle normal that most slicers recompute and throw away. You pay in file size and floating-point noise for data nobody uses.

None of this makes STL useless — a single closed manifold going straight to a slicer is exactly what STL is good at, and its universality is real. But it's the wrong container for a modern print job.

## 3MF: a zipped XML package

3MF (3D Manufacturing Format) was introduced in 2015 by the 3MF Consortium — Microsoft, Autodesk, Dassault Systèmes, HP, Shapeways, and others — specifically to fix these gaps. The clever move is that a `.3mf` isn't a new binary blob; it's a **ZIP archive** following the Open Packaging Conventions (OPC), the same part/relationship scheme used by `.docx` and `.xlsx`. That means you can inspect it with tools you already have:

```console
$ unzip -l model.3mf
Archive:  model.3mf
  Length      Name
---------  ----
      312   [Content_Types].xml
      588   _rels/.rels
    24471   3D/3dmodel.model
     4096   Metadata/thumbnail.png
---------  -------
```

`[Content_Types].xml` maps file extensions to MIME types, `_rels/.rels` points at the primary model part, and `3D/3dmodel.model` is the actual geometry — an XML document. Crack it open and the difference from STL is immediate:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US"
       xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <basematerials id="1">
      <base name="PLA Red" displaycolor="#C81E1EFF"/>
    </basematerials>
    <object id="2" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="10" y="0" z="0"/>
          <vertex x="0" y="10" z="0"/>
          <vertex x="0" y="0" z="10"/>
        </vertices>
        <triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="1" v3="3"/>
        </triangles>
      </mesh>
    </object>
  </resources>
  <build>
    <item objectid="2" transform="1 0 0 0 1 0 0 0 1 50 50 0"/>
  </build>
</model>
```

Three things STL can't express are right there. The root `<model>` carries an explicit **`unit`** attribute (`millimeter` by default; also `micron`, `centimeter`, `inch`, `foot`, `meter`) — no more guessing. Vertices are declared once and triangles reference them **by index** (`v1`/`v2`/`v3`), so shared corners are stored once. And the file separates `<resources>` (reusable objects, materials, colors) from `<build>`, where each `<item>` places an object with a transform matrix. That split is what gives 3MF a real **object/build hierarchy**: many objects, instanced and positioned, in one file, with `<components>` letting objects nest into assemblies.

## Extensions: where 3MF gets serious

The core spec is deliberately minimal; the power lives in versioned **extensions**, activated per-file via `requiredextensions`/`recommendedextensions` on the root element. The official set includes **Materials and Properties**, **Production**, **Beam Lattice**, **Slice**, **Boolean Operations**, **Volumetric**, **Displacement**, and **Secure Content**. Two are worth calling out:

- **Production** adds unique identifiers (UUIDs) to build items and supports splitting a package across multiple model parts — the plumbing a print farm or job-tracking system needs to reference parts unambiguously.
- **Beam Lattice** encodes lattice structures as beams and balls with radii instead of pre-triangulated meshes. A lattice that would be millions of STL triangles becomes a compact node-and-edge graph the slicer expands.

## The practical hook: your slicer already speaks 3MF

Here's the part that matters day to day. When PrusaSlicer, Bambu Studio, or OrcaSlicer offer "Save Project," they write a `.3mf`. Prusa's own docs call it "a complete snapshot of PrusaSlicer" — model geometry *plus* every print, filament, and printer setting, per-object modifiers, custom supports, paint-on seams, and the full plate layout, all stored as extra namespaced parts inside the same ZIP. Reopen it on another machine and you regenerate identical G-code.

That's a genuine round-trip that STL simply can't do. Export a plate to STL and you keep the triangles and lose everything else — the layout, the tuned settings, which object was set to 3 walls. Hand someone a project `.3mf` and they get the whole job. It pairs naturally with the calibration work from the [OrcaSlicer calibration article]({{ site.baseurl }}/articles/cad-3dprint/2026-07-26-orcaslicer-calibration/): the numbers you dial in live in the filament profile, and a project `.3mf` carries them along instead of stranding them on one PC.

On the CAD side the same shift is underway. FreeCAD and Fusion both export 3MF directly, and OpenSCAD writes it too — worth remembering when you're leaning on the [Manifold backend]({{ site.baseurl }}/articles/cad-3dprint/2026-07-31-openscad-manifold-backend/) for clean, watertight output that deserves a container able to carry units and metadata. Most modern slicers now list 3MF as the preferred import format and treat STL as the legacy fallback.

STL isn't going anywhere fast — its ubiquity across every viewer, repair tool, and ancient plugin keeps it alive as the safe interchange default. But for anything richer than a single mute mesh, the ecosystem has already voted with its Save buttons.

**Try next:** rename a slicer project `.3mf` to `.zip`, unzip it, and read `3D/3dmodel.model` alongside the slicer's metadata parts to see exactly which settings your file is carrying.
