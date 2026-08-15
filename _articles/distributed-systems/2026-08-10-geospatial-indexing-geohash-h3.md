---
title: 'Geospatial indexing: why B-trees can''t find nearby drivers'
date: 2026-08-10
track: distributed-systems
summary: 'The ''design Yelp / find nearby drivers'' interview hinges on one fact: a B-tree indexes a single sorted dimension, and proximity is two-dimensional. This walks why range-scanning lat AND lng fails, how geohash flattens 2D to a 1D prefix (and the boundary bug that forces an 8-neighbor query), quadtrees, and the modern hierarchical grids — Google S2 on a Hilbert curve and Uber H3 on hexagons — plus how a radius / k-NN query actually runs against Redis and PostGIS.'
reading_time: 6
tags:
- geospatial
- geohash
- h3
- s2
- spatial-index
- system-design
- quadtree
- redis
- indexing
sources:
- title: 'Uber Engineering — H3: Uber''s Hexagonal Hierarchical Spatial Index'
  url: https://www.uber.com/us/en/blog/h3/
- title: S2Geometry — S2 Cell Hierarchy (official dev guide)
  url: https://s2geometry.io/devguide/s2cell_hierarchy.html
- title: Redis — GEOSEARCH command reference
  url: https://redis.io/docs/latest/commands/geosearch/
- title: PostGIS Manual — KNN and the <-> distance operator
  url: https://postgis.net/docs/geometry_distance_knn.html
- title: Crunchy Data — A Deep Dive into PostGIS Nearest Neighbor Search
  url: https://www.crunchydata.com/blog/a-deep-dive-into-postgis-nearest-neighbor-search
- title: Geohash — Wikipedia
  url: https://en.wikipedia.org/wiki/Geohash
- title: Redis geospatial data type
  url: https://redis.io/docs/latest/develop/data-types/geospatial/
- title: Quadtree — Wikipedia
  url: https://en.wikipedia.org/wiki/Quadtree
---

The prompt is always some flavor of "find the 10 nearest drivers" or "restaurants within 2 km." The candidate reaches for the tool they know — a database index — and hits a wall that is the whole point of the question.

## Why a B-tree can't do proximity

A B-tree indexes **one** sorted dimension. Give it `WHERE ts BETWEEN a AND b` and it does a cheap range scan because rows near each other in value sit near each other in the tree. That is exactly the ordered-locality property that [range partitioning](/articles/distributed-systems/2026-08-10-data-partitioning-sharding) and ordinary [database indexing](/articles/microservices/2026-08-10-database-indexing) rely on.

Proximity breaks it because "near" is two-dimensional. The naive query looks fine:

```sql
WHERE lat BETWEEN 37.76 AND 37.78
  AND lng BETWEEN -122.42 AND -122.40
```

But a composite `(lat, lng)` B-tree sorts by `lat` first, then `lng` only as a tiebreak. So the engine range-scans the `lat` band — every row in a horizontal stripe across the entire globe — and then filters `lng` row by row. You index one dimension and brute-force the other. Two separate single-column indexes are no better: each returns a huge stripe and the planner intersects them. Worse, a degree of longitude is ~111 km at the equator but shrinks toward the poles, so the box isn't even the shape you asked for.

The fix is to **map 2D down to 1D so that closeness in space becomes closeness in the sorted key** — then a B-tree works again. That single idea powers every technique below.

## Geohash: interleave the bits

[Geohash](https://blog.algomaster.io/p/geohashing) encodes a `(lat, lng)` pair into a short base32 string. You binary-search each axis: is the longitude in the west or east half of `[-180, 180]`? Emit a bit, recurse into that half. Do the same for latitude in `[-90, 90]`. Then **interleave**: bit 1 from longitude, bit 2 from latitude, bit 3 longitude, bit 4 latitude, and so on. Group the interleaved bits into 5-bit chunks and map each to the base32 alphabet `0123456789bcdefghjkmnpqrstuvwxyz` (no `a`, `i`, `l`, `o`, to avoid misreads).

```python
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"

def geohash_encode(lat, lng, precision=7):
    lat_range, lng_range = [-90.0, 90.0], [-180.0, 180.0]
    bits, ch, out, even = 0, 0, [], True  # start with longitude
    while len(out) < precision:
        if even:                      # longitude bit
            mid = (lng_range[0] + lng_range[1]) / 2
            if lng >= mid: ch = (ch << 1) | 1; lng_range[0] = mid
            else:          ch = (ch << 1);     lng_range[1] = mid
        else:                         # latitude bit
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid: ch = (ch << 1) | 1; lat_range[0] = mid
            else:          ch = (ch << 1);     lat_range[1] = mid
        even = not even
        bits += 1
        if bits == 5:                 # one base32 char = 5 bits
            out.append(_B32[ch]); bits, ch = 0, 0
    return "".join(out)

geohash_encode(37.7749, -122.4194)  # -> '9q8yyk8' (San Francisco)
```

The payoff: each character narrows the box, so **a shared prefix means the two points sit in the same cell**. `9q8yy` is a ~150 m box; `9q8` is ~150 km. Nearby points usually share a long prefix, and a prefix match is just `WHERE geohash LIKE '9q8yy%'` — a range scan a plain B-tree loves. Precision is roughly: 5 chars ≈ 5 km, 6 chars ≈ 1.2 km, 7 chars ≈ 150 m.

### The boundary caveat every interviewer probes

"Usually" is the trap. Because the bits interleave, a small step in space can flip a **high-order** bit, so two points a few meters apart can land in cells that share almost no prefix — the classic case is straddling the equator, the prime meridian, or any internal cell edge. If you query only the user's own cell, you silently miss the closest restaurant sitting just over the border.

The standard fix: compute the user's geohash cell, then compute its **8 neighbors** (N, S, E, W, and the four diagonals) and query all **9 cells** together. That guarantees anything within one cell-width of the user is in your candidate set regardless of which boundary it hugs.

## Quadtrees: adapt to density

Geohash splits the world on a fixed schedule. A **quadtree** splits on demand: start with one square, and whenever a cell holds more than *k* points, subdivide it into four quadrants; recurse. Dense downtown blocks get deep, fine subdivisions; empty ocean stays one coarse node. That adaptivity keeps each leaf's candidate count bounded — good when density is wildly uneven — at the cost of an in-memory pointer tree rather than a flat sortable string, so it's harder to shard across nodes than a prefix key.

## S2 and H3: hierarchical grids done right

Two modern systems fix geohash's rectangular-in-degrees distortion by working on the sphere.

**Google S2** projects the sphere onto the **six faces of a cube**, then recursively subdivides each face into four children, down through 31 levels (0–30); a level-30 leaf cell is about 1 cm across. The clever part is the ordering: S2 threads a **Hilbert space-filling curve** through the cells — "six Hilbert curves linked together to form a single continuous loop over the entire sphere" — and numbers them along it into a single 64-bit `S2CellId`. The Hilbert curve preserves locality better than geohash's Z-order interleave: *if two cell IDs are close, the cells are close*. A region query becomes a small set of `[start, end]` ID ranges — again, B-tree-friendly. This is the scheme used to index geo data in systems like Cloud Spanner.

**Uber H3** tiles the world with **hexagons** across 16 resolutions, each finer level having ~1/7 the area of the coarser one, encoded as a 64-bit cell ID that truncates to its parent. Why hexagons beat squares? **Uniform neighbor distance.** A hexagon has exactly one distance from its center to every one of its 6 neighbors. A square has two (edges vs. diagonals); a triangle has three. Uber's own words: this "greatly simplifies performing analysis and smoothing over gradients" — ideal for expanding a driver search ring outward evenly. The catch: you can't tile a sphere (an icosahedron) with hexagons alone, so H3 adds **12 pentagons**, which Uber positions over ocean so they rarely disturb real queries.

| Approach | Shape | Ordering | Cell ID | Note |
|---|---|---|---|---|
| Geohash | lat/lng rect | Z-order interleave | base32 string | boundary jumps → query 9 cells |
| Quadtree | square | tree pointers | path | adapts to density |
| S2 | quad on cube face | Hilbert curve | 64-bit | best locality; range queries |
| H3 | hexagon | hierarchical | 64-bit | uniform neighbors; 12 pentagons |

## How a radius / k-NN query actually runs

Every system follows the same three-step shape:

1. **Compute the cell(s).** Encode the query point at a resolution matching your radius, and gather that cell plus its neighbors (the 8 geohash neighbors, or H3's `gridDisk(k)` ring, or S2's covering ranges).
2. **Fetch candidates.** Pull every point whose cell is in that set — one indexed prefix/range lookup, a coarse over-approximation of the circle.
3. **Refine by exact distance.** Compute true great-circle (Haversine) distance for each candidate, drop anything outside the radius, and sort; for k-NN, take the top k.

**Redis** ships this out of the box. `GEOADD` stores members in a sorted set keyed by a **52-bit geohash-encoded score**; `GEOSEARCH ... FROMLONLAT lng lat BYRADIUS 2 km ASC` scans the grid-aligned box around the target and returns members inside the shape — documented complexity `O(N + log(M))`, N being the box population. It's the candidate-then-refine flow, productized.

**PostGIS** takes the R-tree route: a **GiST index** (an R-tree) on the geometry column bounds each row by its rectangle. `ST_DWithin(geom, point, 2000)` uses that index for radius search, and the **`<->` KNN operator** in `ORDER BY geom <-> point LIMIT 10` triggers a best-first index descent that visits nodes in order of potential distance — Crunchy Data measured a nearest-neighbor query drop from 14 s (seq scan + sort) to 7.8 ms, roughly 1,800× faster.

The interview answer, compressed: B-trees index one sorted axis, proximity needs two, so you flatten 2D to a locality-preserving 1D key (geohash / S2 / H3), query a cell plus its neighbors to beat the boundary bug, then refine survivors by exact distance.

**Try next:** implement `gridDisk(k)` neighbor expansion for a variable-radius search, and benchmark H3 vs. a geohash-prefix scan on skewed, downtown-heavy driver data.
