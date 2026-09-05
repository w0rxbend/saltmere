---
title: "G2/G3 Arcs in G-code: ArcWelder, Slicer Arc Fitting, and What the Firmware Does With Them"
date: 2026-08-27
track: cad-3dprint
summary: "Slicers tessellate every curve into chains of short G1 moves; arc fitting collapses those chains back into G2/G3 arc commands within a stated deviation tolerance. This article derives the sagitta bound that governs the fit, walks ArcWelder's incremental fitting loop, shows how Marlin and Klipper re-interpolate the arc into segments on arrival, and identifies the one configuration where the round trip pays: a serial-bandwidth-starved Marlin board."
reading_time: 7
tags: [gcode, arc-welder, marlin, klipper, slicing, firmware]
sources:
  - title: "ArcWelderLib — Anti-Stutter and GCode Compression (GitHub)"
    url: "https://github.com/FormerLurker/ArcWelderLib"
  - title: "Klipper configuration reference — [gcode_arcs]"
    url: "https://www.klipper3d.org/Config_Reference.html"
  - title: "Marlin Configuration_adv.h — ARC_SUPPORT (2.1.x)"
    url: "https://github.com/MarlinFirmware/Marlin/blob/2.1.x/Marlin/Configuration_adv.h"
  - title: "PrusaSlicer 2.7.0 release notes — arc fitting based on ArcWelderLib"
    url: "https://github.com/prusa3d/PrusaSlicer/releases/tag/version_2.7.0-beta1"
---

**Gist.** A slicer stores no curves: every circle, fillet, and organic perimeter leaves the slicer as a chain of short `G1` straight-line moves. Arc fitting — ArcWelder as a post-processor, or the equivalent built into PrusaSlicer 2.7.0 and later — detects chains that lie within a tolerance of a circular arc and replaces them with a single `G2` (clockwise) or `G3` (counter-clockwise) command, shrinking the file and the command count. The firmware then does the inverse: both Marlin and Klipper **re-interpolate the arc back into straight segments** before planning, so the arc is a transport encoding, not a motion primitive. The round trip buys real print-quality improvement only where the transport is the bottleneck — classically a Marlin board fed over a serial link — and costs resolution anywhere the firmware's re-segmentation is coarser than the segments the slicer originally emitted.

## The tolerance math: sagitta bounds both directions

Both the fitting step and the firmware's re-segmentation are governed by the same quantity: the **sagitta**, the maximum distance between a circular arc and the chord that subtends it. For a chord of length $c$ on a circle of radius $r$, the sagitta is

$$s = r - \sqrt{r^2 - (c/2)^2} \;\approx\; \frac{c^2}{8r} \quad (c \ll r).$$

The approximation follows from the binomial expansion of the square root and is accurate to second order. It can be read in two directions:

- **Fitting (slicer → arc).** Replacing a polyline with an arc moves each original vertex by at most the fitting tolerance, and points between vertices by at most the sagitta of the local chord. ArcWelder's *resolution* parameter — **default 0.05 mm, interpreted as ±0.025 mm** — is exactly this deviation budget: every source point must lie within half the resolution of the fitted circle, or the arc is not emitted.
- **Interpolation (arc → firmware segments).** A firmware that chops an arc into chords of length $c$ commits a mid-chord error of $c^2/8r$. Solving for the chord length that keeps the error under a budget $s$ gives $c = \sqrt{8rs}$: a **10 mm-radius arc rendered with 1 mm chords deviates by about 0.0125 mm; the same chords on a 1 mm radius deviate by 0.125 mm** — worse than ArcWelder's entire fitting budget. Small radii are where fixed-chord re-interpolation loses.

The composition of the two steps is the real tolerance of the pipeline: fitting error plus re-interpolation error, and the two do not cancel.

## The ArcWelder fitting loop

ArcWelder streams the G-code and maintains a candidate arc over a growing window of consecutive extrusion moves. The loop is incremental:

1. Append the next `G1` endpoint to the window.
2. Fit a circle to the window's points and check that **every point lies within the deviation budget** of that circle, that the points advance monotonically along it (no doubling back), and that the arc's length stays within the *path tolerance* — **default 5 %** — of the summed segment lengths. The length check is a safety net against a fit that is pointwise close but traverses a materially different path.
3. On the first endpoint that breaks the fit, emit the longest valid arc as `G2`/`G3` with an `I`/`J` center offset, and restart the window at the failing segment.

Straight chains are excluded — the tool documents **built-in detection that prevents collinear lines from becoming arcs**, since a near-infinite radius makes the `I`/`J` offsets numerically ill-conditioned. The project reports compression ratios around 2× on curvature-heavy files (one documented run: ratio 2.27, a 56 % size reduction); a boxy, straight-walled model compresses barely at all, because there is nothing to fit.

PrusaSlicer 2.7.0 folded the same capability into the slicer itself — the release notes state the arc-fitting feature is **based on ArcWelderLib** — which removes the post-processing step but not any of the semantics below.

### Implementation sketch (Scala)

The load-bearing idea — grow a window until the sagitta budget breaks — fits in a page. Circle fitting is reduced here to the circumcircle of the window's endpoints and midpoint, which is enough to make the acceptance test concrete:

```scala
final case class P(x: Double, y: Double)
final case class Circle(cx: Double, cy: Double, r: Double):
  def deviation(p: P): Double =
    math.abs(math.hypot(p.x - cx, p.y - cy) - r)

def fitCircle(a: P, mid: P, b: P): Option[Circle] =
  val d = 2.0 * (a.x * (mid.y - b.y) + mid.x * (b.y - a.y) + b.x * (a.y - mid.y))
  if math.abs(d) < 1e-9 then None            // collinear: no finite radius
  else
    val (sa, sm, sb) = (a.x*a.x + a.y*a.y, mid.x*mid.x + mid.y*mid.y, b.x*b.x + b.y*b.y)
    val cx = (sa * (mid.y - b.y) + sm * (b.y - a.y) + sb * (a.y - mid.y)) / d
    val cy = (sa * (b.x - mid.x) + sm * (a.x - b.x) + sb * (mid.x - a.x)) / d
    Some(Circle(cx, cy, math.hypot(a.x - cx, a.y - cy)))

/** Longest prefix of `pts` that one arc covers within ±tol; None if under 3 points fit. */
def growArc(pts: Vector[P], tol: Double): Option[(Circle, Int)] =
  var best: Option[(Circle, Int)] = None
  var n = 3
  var ok = true
  while ok && n <= pts.length do
    val window = pts.take(n)
    fitCircle(window.head, window(n / 2), window.last) match
      case Some(c) if window.forall(c.deviation(_) <= tol) =>
        best = Some((c, n)); n += 1
      case _ => ok = false                    // budget broken: emit `best`, restart window
  best
```

A production fitter (ArcWelder included) must additionally carry extrusion (`E`) forward proportionally along the arc, verify direction and monotone progress, and enforce the path-length tolerance — omitted here because the acceptance test, not the bookkeeping, is the mechanism.

## What the firmware does with the arc

**Marlin** enables `G2`/`G3` with `ARC_SUPPORT` and immediately converts each arc into planner segments. The defaults in `Configuration_adv.h` (2.1.x) name the policy: segment length between **`MIN_ARC_SEGMENT_MM` 0.1 mm and `MAX_ARC_SEGMENT_MM` 1.0 mm**, at least **`MIN_CIRCLE_SEGMENTS` 72** segments per full circle, an optional feedrate-driven mode (`ARC_SEGMENTS_PER_SEC`), and **`N_ARC_CORRECTION` 25** — the number of segments computed with a fast incremental rotation (a small-angle approximation) before the position is re-derived exactly to stop the accumulated drift. The arc therefore becomes 0.1–1 mm chords inside the board, regardless of how fine the slicer's original segments were.

**Klipper** is explicit that arcs are not native: the `[gcode_arcs]` section's only tuning knob is `resolution`, and the documentation states that **an arc will be split into segments of the given length, default 1.0 mm**, with arcs smaller than the resolution becoming straight lines. This is deliberate architecture — Klipper's host process plans motion from linear moves and applies the same lookahead and junction handling to the generated chords as to any `G1` stream. The Klipper documentation records what the module does, not a design rationale beyond it; what is observable is that with the default resolution, **feeding Klipper arc-fitted G-code can yield coarser toolpaths than the un-fitted file**, whose slicer segments on small perimeters are typically much shorter than 1 mm.

## When the round trip helps, and when it is a loss

The decisive variable is **where the command stream bottlenecks**.

- **Serial-fed Marlin: helps.** A board receiving G-code over a serial link (OctoPrint over USB is the canonical case) can be starved when a curve dissolves into many very short `G1` lines: commands arrive more slowly than the planner drains them, the buffer empties, and the print stutters and blobs on curved walls. ArcWelder's stated purpose is precisely this anti-stutter case — one `G2` line replaces dozens of `G1` lines, cutting the characters per millimetre of toolpath that must cross the link. The firmware's re-interpolation then happens on-board, where no bandwidth limit applies.
- **SD-card Marlin: mostly a file-size feature.** Reading from SD removes the serial link, so the transport argument weakens to faster file transfers and smaller files. Whether command *parsing* remains a bottleneck depends on the board; no general claim is made here.
- **Klipper: a no-op at best, a resolution loss at worst.** The host is a full computer parsing from local storage — the bandwidth starvation the arcs were invented to relieve does not exist, and the default 1 mm re-segmentation can be coarser than the slicer's original tessellation. Arc-fitted input to Klipper trades away resolution for a compression benefit the architecture never needed. Lowering `resolution` narrows the loss at the cost of more generated moves.
- **Small radii anywhere: check the composition.** By the sagitta bound, a 1 mm chord on a 1 mm radius already deviates 0.125 mm; fitting tolerance stacks on top. ArcWelder ships firmware-compensation options for firmwares that mishandle small-radius arcs, which is itself evidence that this regime is the fragile one.

## Pitfalls

- **Arc-fitted G-code prints visibly faceted curves on Klipper at default settings.** `[gcode_arcs]` re-segments every arc into 1.0 mm chords, which on small perimeters is coarser than the segments the slicer originally emitted.
- **The pipeline's total deviation is fitting error plus re-interpolation error.** Judging accuracy from ArcWelder's ±0.025 mm alone ignores the firmware's chord error of ≈ c²/8r, which dominates at small radii.
- **G-code previewers lie about arcs.** The ArcWelder documentation warns that most visualizers do not correctly display `G2`/`G3`, so a mangled-looking preview is not evidence of a mangled toolpath — and a clean preview is not evidence of a correct one.
- **In PrusaSlicer, enabling the pressure equalizer suppresses arc output.** Issue #11828 reports that with a non-zero pressure equalizer the exported file contains only `G1` segments, so arc fitting silently becomes a no-op.
- **Sending `G2`/`G3` to a firmware without arc support fails at the parser.** Marlin requires `ARC_SUPPORT` and Klipper requires `[gcode_arcs]`; without them the command is unknown, not approximated.
- **Raising ArcWelder's resolution buys compression with geometry.** The parameter is the deviation budget itself; the documentation recommends against values above 0.1 mm because every fitted arc is then allowed to stray that far from the sliced path.
