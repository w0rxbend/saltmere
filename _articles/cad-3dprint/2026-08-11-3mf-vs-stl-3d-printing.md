---
title: "3MF vs STL: from triangle soup to a structured print package"
date: 2026-08-11
track: cad-3dprint
summary: "STL is a flat list of triangles with no units, colours, or metadata. 3MF is a ZIP/XML package with an explicit unit attribute, an indexed mesh, an object/build hierarchy, materials, and versioned extensions — and modern slicers use it to round-trip an entire print job in one file. This article dissects the container and the guarantees each format can and cannot make."
reading_time: 7
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

**Gist.** A print job is more than a surface: it carries units, several objects, their placement, their materials, and the settings that produced the toolpaths, yet STL (stereolithography format) encodes only an unindexed list of triangles and therefore loses all of it at every export. 3MF (3D Manufacturing Format) replaces that flat list with a ZIP archive of XML parts that declares a unit, stores vertices once and references them by index, and separates reusable resources from a build section that instances and positions them. The cost is a container that no longer parses in twenty lines: readers must handle ZIP packaging, XML namespaces, relationship parts, and a per-file declaration of which extensions are required.

## What STL stores, and what it cannot

An STL triangle is three vertices and a face normal. That is the entire vocabulary, and it fixes what the format cannot express:

- **Units.** A coordinate of `10` may denote 10 mm or 10 inches; the file never states which. Every import applies an assumed scale.
- **Colour or material.** No per-face colour and no material assignment, so a multi-material machine receives no assignment data.
- **Multiple objects.** One STL file is one mesh. A plate of five parts is either five files or a single merged mesh whose components are no longer separable by construction.
- **Per-object settings, metadata, provenance.** No author, no licence, no per-object layer height.

The encoding is also redundant. **STL stores each triangle as three full vertices, so a corner shared by several triangles is written once per triangle with no index table.** The binary variant additionally stores a per-triangle normal that many slicers recompute from the winding order and discard. Both cost file size, and the repeated coordinates are a source of floating-point mismatch: two copies of a shared corner that differ in the last bits describe two distinct points, which is how a mesh that looks closed acquires cracks that repair tools must weld shut.

None of this makes STL unusable. A single closed manifold destined directly for a slicer is precisely the case STL covers, and its support across viewers and repair tools is real. It is the wrong container for a job with structure.

## The 3MF package

3MF was introduced in 2015 by the 3MF Consortium, whose members include Microsoft, Autodesk, Dassault Systèmes, HP, and Shapeways. A `.3mf` is not a new binary encoding: it is a **ZIP archive following the Open Packaging Conventions (OPC)**, the part-and-relationship scheme also used by `.docx` and `.xlsx`. Standard archive tools therefore inspect it directly:

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

`[Content_Types].xml` maps extensions to MIME types, `_rels/.rels` identifies the primary model part, and `3D/3dmodel.model` holds the geometry as XML:

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

Three properties absent from STL appear in that fragment. The root `<model>` element carries an explicit **`unit` attribute** — `millimeter` by default, with `micron`, `centimeter`, `inch`, `foot`, and `meter` also defined — so scale is stated rather than assumed. Vertices are declared once and each triangle names them **by index** through `v1`/`v2`/`v3`, which makes a shared corner exactly one coordinate triple: the identity of a corner is now the index, not an approximate coordinate comparison. And the document separates `<resources>`, which holds reusable objects, materials and colours, from `<build>`, where each `<item>` places an object under a transform matrix. That separation is the **object/build hierarchy**: one object resource may be instanced several times at different positions, and `<components>` lets objects nest to form assemblies.

## Extensions and the required/recommended split

The core specification is small; further capability is defined in versioned **extensions**, declared per file through the `requiredextensions` and `recommendedextensions` attributes on the root element. The distinction is the interoperability contract: an extension listed as required is one a consumer must understand to process the file correctly, so a reader lacking it should reject the file rather than silently produce a partial part. The published set includes **Materials and Properties**, **Production**, **Beam Lattice**, **Slice**, **Boolean Operations**, **Volumetric**, **Displacement**, and **Secure Content**. Two illustrate the range:

- **Production** adds universally unique identifiers (UUIDs) to build items and allows a package to be split across several model parts, so a part can be referenced unambiguously across a job-tracking system.
- **Beam Lattice** encodes lattice structures as beams and balls with radii instead of pre-triangulated surfaces, so a lattice that would occupy a large triangle count is carried as a node-and-edge graph that the consumer expands.

### Implementation sketch

Converting triangle soup to an indexed mesh is the step that turns an approximate coordinate into an identity. The load-bearing decision is the key: exact bit patterns weld only vertices that are already identical, whereas quantising to a tolerance welds near-coincident corners and can collapse genuine thin features.

```python
import struct

def read_binary_stl(path):
    """Yield (x, y, z) corner triples; the per-triangle normal is discarded."""
    with open(path, "rb") as f:
        f.read(80)                                  # header, no defined meaning
        (count,) = struct.unpack("<I", f.read(4))
        for _ in range(count):
            vals = struct.unpack("<12fH", f.read(50))
            yield tuple(vals[3:6]), tuple(vals[6:9]), tuple(vals[9:12])

def index_mesh(soup, tol=1e-4):
    verts, seen, tris = [], {}, []
    for corner_triple in soup:
        ids = []
        for v in corner_triple:
            k = tuple(round(c / tol) for c in v)    # grid key, not raw floats
            if k not in seen:
                seen[k] = len(verts)
                verts.append(v)
            ids.append(seen[k])
        # A repeated index means two corners were welded into one vertex.
        if len(set(ids)) == 3:
            tris.append(tuple(ids))
    return verts, tris
```

## Round-tripping a print job

PrusaSlicer, Bambu Studio, and OrcaSlicer write `.3mf` for the "Save Project" action. Prusa's documentation describes the project file as carrying the model geometry together with the print, filament and printer settings and the placement of the objects on the bed, held as additional namespaced parts inside the same ZIP archive. Reopening that package restores the slicer to the state it was saved in, rather than to a mesh plus whatever profile happens to be loaded.

STL cannot carry that state. **Exporting a plate to STL preserves the triangles and discards the layout, the tuned settings, and any per-object override.** The project package is what makes calibration transferable: the values obtained in the [OrcaSlicer calibration article]({{ site.baseurl }}/articles/cad-3dprint/2026-07-26-orcaslicer-calibration/) reside in the filament profile, and a project `.3mf` carries them rather than leaving them on one workstation.

The same shift appears upstream. FreeCAD, Fusion, and OpenSCAD export 3MF directly — relevant when relying on the [Manifold backend]({{ site.baseurl }}/articles/cad-3dprint/2026-07-31-openscad-manifold-backend/) for watertight output, since the container can then state the unit and retain metadata. Many current slicers list 3MF as the preferred import format and STL as the legacy fallback.

STL retains its role as the interchange default because every viewer, repair tool, and older plugin reads it. For anything beyond a single unannotated mesh, the structured package is what the toolchain now writes by default.

## Pitfalls

- **Renaming a project `.3mf` to `.stl` for a tool that demands STL discards the build section**: the geometry survives, but placement, per-object settings and material assignment have no representation in the target format.
- **Assuming millimetres when a `unit` attribute says otherwise** produces a part scaled by the ratio between the declared and assumed unit; the file is valid and the error surfaces only on the plate.
- **Ignoring `requiredextensions` and parsing the core geometry anyway** yields a file that opens and prints wrong — beam-lattice content, for instance, carries structure the core mesh elements do not describe.
- **Welding vertices with too coarse a tolerance during STL import** collapses thin walls and short edges into degenerate triangles, which then appear as holes after the degenerate faces are dropped.
- **Treating a slicer's project `.3mf` as a portable geometry exchange file** fails across vendors: the settings parts are vendor-namespaced, so another slicer reads the geometry and disregards the profile it did not write.
- **Editing `3D/3dmodel.model` inside the archive and rezipping with default options** can break consumers that rely on the Open Packaging Conventions part naming and relationship entries surviving intact.
