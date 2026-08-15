---
title: "FreeCAD FEM Workbench: Predicting Whether a Printed Bracket Flexes or Snaps"
date: 2026-08-15
track: cad-3dprint
summary: "Running a load-bearing bracket through FreeCAD's finite element method workbench: realistic PLA properties, one fixed face, one loaded face, a Gmsh tetrahedral mesh, a CalculiX static solve, and von Mises and displacement fields — followed by derating, because printed parts fail at layer lines rather than where isotropic theory predicts."
reading_time: 6
tags: [freecad, fem, calculix, gmsh, simulation, pla, petg, 3d-printing]
sources:
  - title: "FreeCAD Wiki: FEM tutorial"
    url: "https://wiki.freecad.org/FEM_tutorial"
  - title: "Getting started with FEM — FreeCAD News"
    url: "https://blog.freecad.org/2025/09/16/getting-started-with-fem/"
  - title: "What's new in FEM for FreeCAD 1.1? — FreeCAD News"
    url: "https://blog.freecad.org/2025/09/09/what-is-new-in-fem-for-freecad-1-1/"
  - title: "UltiMaker: PETG vs PLA vs ABS — 3D printing strength comparison"
    url: "https://ultimaker.com/learn/petg-vs-pla-vs-abs-3d-printing-strength-comparison/"
  - title: "CNC Kitchen: Comparing PLA, PETG & ASA (feat. Prusament) — measured stiffness and layer adhesion"
    url: "https://www.cnckitchen.com/blog/comparing-pla-petg-amp-asa-feat-prusament"
---

**Gist.** The default hobby method for validating a load-bearing print is to print it, load it, and observe whether it survives — a test that costs hours of filament per iteration and yields one bit of information. FreeCAD's finite element method (FEM) workbench replaces that iteration with a static linear-elastic solve: a tetrahedral mesh, a fixed boundary condition, an applied force, and a stress field that identifies both the failure location and the approximate failure load. The cost is that the solver assumes an isotropic, void-free continuum, which a fused deposition modelling (FDM) part is not, so every result must be derated by a factor the simulation cannot compute.

## The pipeline and its objects

FEM in FreeCAD is a fixed dependency chain rather than a free-form tool. A solid body is placed inside an **Analysis container**; under that container hang a **material** assignment, **boundary conditions** (which degrees of freedom are held), **loads** (which forces are applied), a **mesh** produced by Gmsh, and a **solver** object driving CalculiX. Running the solver writes a results object that the post-processing view renders as von Mises stress and displacement fields. Gmsh and CalculiX ship with the standard FreeCAD installers, so no external solver installation is required for basic structural work.

This article is current for **FreeCAD 1.1**. The FEM workbench has been reworked across the 1.x releases — the separation of loads from boundary conditions, and a post-processing pipeline built from filters — so older tutorials name objects that no longer exist under those names.

## Worked case: a PLA shelf bracket

The geometry is an L-bracket modelled in Part Design: two 60 mm legs, 40 mm wide, 4 mm thick, with a fillet at the inner corner. The design load is a 5 kg shelf, giving **50 N** acting downward near the tip of the horizontal leg.

1. **Analysis container.** In the FEM workbench, select the body and create the container. Every subsequent object nests inside it.
2. **Material.** Add *Material for solid*. The bundled thermoplastic cards carry bulk-plastic values, so the card is edited (or replaced by a custom one) with values measured on printed specimens: for PLA, Young's modulus of order **3000 MPa**, Poisson ratio **0.33**, density **1.24 g/cm³**. PETG is assigned a markedly lower modulus; CNC Kitchen's comparison measures PETG as the less stiff of the two while its strength is comparable.
3. **Fixed boundary condition.** The back face of the vertical leg is constrained. Constraining only the two screw-hole cylinders is the more faithful representation of a bolted joint and produces higher local stress.
4. **Force load.** A *Force load* of **50 N** in the −Z direction is applied to the outer 20 mm of the horizontal leg's top face. FreeCAD distributes the total force over the selected face.
5. **Mesh.** *Mesh from shape* with Gmsh, maximum element size **2 mm**, which places a small number of second-order tetrahedra through the 4 mm wall. A faceted-looking fillet is corrected with a local mesh refinement rather than by reducing the global element size.
6. **Solve.** The CalculiX solver object runs a *Static* analysis; a mesh of this size completes in seconds.

## Interpreting the two result fields

In 1.1 a post-processing *pipeline* object appears after a successful run; earlier versions expose a `CCX_Results` object shown via *Show result*. Two fields carry the decision.

**Von Mises stress** reduces the full three-dimensional stress tensor to a single scalar comparable against a uniaxial tensile strength. For this bracket the peak sits in the high 20s of MPa, concentrated at the inner corner. That agrees with elementary beam theory: a moment of 50 N × 60 mm on a section of second moment of area *I* = 40 × 4³/12 mm⁴ yields approximately **28 MPa** of bending stress at the root. **The agreement holds only because of the fillet** — a sharp inner corner drives the computed peak well above the beam-theory value, since the stress concentration at a reentrant corner is a property of the geometry, not of the mesh.

**Displacement magnitude** shows the tip deflecting roughly **5 mm**. No stress limit is exceeded, but a shelf that visibly sags is a failed part in service. The deformation scale slider exaggerates the deformed shape; if the exaggerated shape does not bend in the expected sense, a boundary condition has been applied to the wrong face.

Whether 28 MPa is acceptable depends on which strength is used. Bulk PLA tensile strength is 50–60 MPa, which would pass the part. A printed bracket is judged against a lower allowable, derived below. Increasing the wall to 6 mm drops the peak to approximately **12.5 MPa** and the deflection to under **2 mm** — the cantilever deflection falls with the cube of the thickness while the stress falls only with its square; because only the pad dimension changes, the entire analysis tree stays attached and the solve can be repeated directly.

## Why derating is mandatory

Every number CalculiX produces assumes an isotropic, void-free solid. An FDM part violates that assumption in three independent ways.

- **Anisotropy.** Strength measured across layer lines (the Z direction, in the usual print orientation) is a fraction of in-plane strength, because the load is carried by fused interfaces between beads rather than by continuous polymer. Published layer-adhesion comparisons put that fraction below half for common filaments, but no single number covers every material and profile.
- **Layer adhesion is process-dependent.** Nozzle temperature, part cooling, and extrusion width all move Z-strength, and they move it enough that two prints of the same file on the same machine are not interchangeable specimens. The solver has no representation of the slicer profile that produced the part.
- **Infill.** The simulated body is solid; a printed part typically is not. Structural parts either print at 100% infill with four or more perimeters, or the FEM stress is treated as optimistic by a further unquantified factor.

The resulting procedure: orient the print so that layer lines are **not perpendicular to the peak tensile stress** the solve reports — for this bracket, lying on its side — take at most **half of bulk strength** as the across-layer allowable, and apply a safety factor of 2 on top. For PLA that caps peak von Mises stress at roughly **13–15 MPa** in the orientation-critical direction. The 4 mm bracket fails that criterion; the 6 mm bracket passes.

A useful follow-up experiment: re-run with the fixed condition applied to the screw holes alone rather than the whole back face, and observe the von Mises peak migrate to the upper screw; adding a countersunk boss then quantifies the reduction.

## Pitfalls

- **A sharp inner corner produces a stress peak that grows as the mesh is refined.** The linear-elastic solution at a reentrant corner is singular, so refinement reports ever-higher stress rather than converging; the fillet is what makes the number meaningful.
- **Fixing the entire back face understates local stress.** A fully constrained face carries load everywhere, whereas a bolted bracket transfers it through the screw holes, where the true peak sits.
- **The bundled thermoplastic material cards are bulk values.** Solving with an unedited PLA card overstates both stiffness and allowable strength relative to a printed specimen.
- **Passing the stress check while failing the deflection check.** A part with peak stress far below the allowable can still deflect several millimetres at the tip, which is a functional failure for a shelf.
- **A global element size larger than roughly half the wall thickness leaves too few elements through the thickness** to resolve the bending stress gradient, biasing the reported peak low.
- **Comparing von Mises stress against a Z-direction allowable while the part is printed in another orientation.** Von Mises is a scalar with no direction; the orientation-dependent allowable only applies once the print orientation relative to the principal tensile direction is fixed.
