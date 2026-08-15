---
title: 'Geospatial indexing: why B-trees cannot find nearby drivers'
date: 2026-08-10
track: distributed-systems
summary: 'A B-tree indexes a single sorted dimension; proximity is two-dimensional. Why range-scanning latitude and longitude together fails, how geohash flattens 2D to a 1D prefix (and the boundary defect that forces an 8-neighbour query), quadtrees, and the hierarchical grids — Google S2 on a Hilbert curve, Uber H3 on hexagons — plus the execution shape of a radius or k-nearest-neighbour query in Redis and PostGIS.'
reading_time: 7
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

**Gist.** A B-tree orders records along one dimension, so it can answer "value between *a* and *b*" cheaply, but proximity on a sphere is two-dimensional and has no such total order. Spatial indexes therefore map two dimensions onto one **locality-preserving key** — a geohash prefix, an S2 cell identifier along a Hilbert curve, an H3 hexagon identifier — so that a range scan retrieves a superset of the neighbourhood. The cost is that the mapping is approximate: the retrieved cells over-cover the query circle, points a few metres apart can fall in cells with no shared prefix, and every query needs a second exact-distance pass over the candidates.

## Why a B-tree cannot answer proximity

A B-tree indexes **one** sorted dimension. `WHERE ts BETWEEN a AND b` becomes a cheap range scan because records adjacent in value are adjacent in the tree — the ordered-locality property that [range partitioning](/articles/distributed-systems/2026-08-10-data-partitioning-sharding) and ordinary [database indexing](/articles/microservices/2026-08-10-database-indexing) depend on.

The naive proximity query looks like a range scan:

```sql
WHERE lat BETWEEN 37.76 AND 37.78
  AND lng BETWEEN -122.42 AND -122.40
```

A composite `(lat, lng)` B-tree sorts by `lat` first and uses `lng` only as a tiebreak within equal latitudes. The engine therefore scans the whole `lat` band — **a horizontal stripe around the entire globe** — and filters `lng` row by row. One dimension is indexed, the other is brute-forced. Two separate single-column indexes do not help: each yields a large stripe and the planner intersects them. The box is also not the shape the query implies, because a degree of longitude spans roughly 111 km at the equator and contracts toward the poles.

The remedy is to **map two dimensions onto one key such that closeness in space implies closeness in the sorted key**. Every technique below is a variant of that mapping.

## Geohash: interleaved bits as a prefix key

[Geohash](https://blog.algomaster.io/p/geohashing) encodes a `(lat, lng)` pair as a base32 string. Each axis is binary-searched: longitude against the midpoint of `[-180, 180]`, latitude against the midpoint of `[-90, 90]`, one bit emitted per comparison, recursing into the chosen half. The bits are then **interleaved** — longitude, latitude, longitude, latitude — grouped into 5-bit chunks, and mapped through the alphabet `0123456789bcdefghjkmnpqrstuvwxyz`, which omits `a`, `i`, `l` and `o`.

Each character narrows the cell, so **a shared prefix places two points in the same cell**: `9q8yy` is a box of roughly 5 km on a side, `9q8` roughly 150 km. Prefix matching reduces to `WHERE geohash LIKE '9q8yy%'`, a range scan on an ordinary B-tree. Approximate cell widths: 5 characters ≈ 5 km, 6 ≈ 1.2 km, 7 ≈ 150 m.

### The boundary defect

The prefix property holds *usually*, not always. Because the bits interleave, a small displacement in space can flip a **high-order** bit, so two points metres apart can occupy cells sharing almost no prefix. The visible cases are points straddling the equator, the prime meridian, or any internal cell edge. A query restricted to the caller's own cell silently omits a nearer candidate immediately across the border, and the omission is invisible: the query returns results, merely the wrong ones.

The standard mitigation computes the caller's cell and its **8 neighbours** (north, south, east, west and the four diagonals) and scans all **9 cells**. Anything within one cell-width of the query point is then in the candidate set irrespective of which boundary it sits against.

## Quadtrees: subdivision driven by density

Geohash subdivides on a fixed schedule. A **quadtree** subdivides on demand: begin with one square and, whenever a cell holds more than *k* points, split it into four quadrants and recurse. Dense urban cells become deep; empty ocean remains a single coarse node. Leaf candidate counts stay bounded under skewed density, at the cost of an in-memory pointer structure rather than a flat sortable key — which makes sharding across nodes harder than with a prefix key.

## S2 and H3: hierarchical grids on the sphere

**Google S2** projects the sphere onto the **six faces of a cube** and recursively subdivides each face into four children through 31 levels (0–30); a level-30 leaf cell covers roughly a square centimetre. The ordering is the distinguishing part: S2 threads a **Hilbert space-filling curve** through the cells — one curve per cube face, joined into a single continuous curve over the whole sphere — and numbers them along that curve into a single 64-bit `S2CellId`. The Hilbert order preserves locality more strongly than geohash's Z-order interleave: cells with nearby identifiers are nearby in space. A region query becomes a small set of `[start, end]` identifier ranges, again B-tree-friendly.

**Uber H3** tiles the world with **hexagons** across 16 resolutions, each finer resolution having approximately 1/7 the cell area of the coarser one, encoded as a 64-bit identifier that truncates to its parent. The property Uber records for hexagons is **uniform neighbour distance**: a hexagon has one centre-to-centre distance to each of its 6 neighbours, whereas a square has two (edge versus diagonal) and a triangle three, which Uber describes as simplifying analysis and smoothing over gradients. A sphere cannot be tiled by hexagons alone, so H3 introduces **12 pentagons**, placed over water where possible.

| Approach | Shape | Ordering | Cell ID | Note |
|---|---|---|---|---|
| Geohash | lat/lng rect | Z-order interleave | base32 string | boundary jumps → query 9 cells |
| Quadtree | square | tree pointers | path | adapts to density |
| S2 | quad on cube face | Hilbert curve | 64-bit | best locality; range queries |
| H3 | hexagon | hierarchical | 64-bit | uniform neighbours; 12 pentagons |

## Execution shape of a radius or k-NN query

Every system follows the same three phases:

1. **Cell computation.** Encode the query point at a resolution matched to the radius and gather that cell plus its neighbours — the 8 geohash neighbours, H3's `gridDisk(k)` ring, or an S2 covering.
2. **Candidate fetch.** Retrieve every point whose cell falls in that set with one indexed prefix or range lookup. The result is a coarse over-approximation of the circle.
3. **Exact refinement.** Compute true great-circle (Haversine) distance per candidate, discard points outside the radius, sort, and for k-nearest-neighbour (k-NN) keep the first k.

**Redis** implements this directly. `GEOADD` stores members in a sorted set under a **52-bit geohash-derived score**; `GEOSEARCH ... FROMLONLAT lng lat BYRADIUS 2 km ASC` scans the grid-aligned box around the target and returns members inside the requested shape, with documented complexity `O(N + log(M))` where N is the number of elements in the grid-aligned bounding box around the requested shape and M the number of items inside the shape.

**PostGIS** takes the R-tree route: a **GiST index** bounds each row by its rectangle. `ST_DWithin(geom, point, 2000)` uses that index for radius search, and the **`<->` KNN operator** in `ORDER BY geom <-> point LIMIT 10` triggers a best-first index descent visiting nodes in order of potential distance, so the scan stops once k rows are emitted instead of computing a distance for every row and sorting. Crunchy Data's walkthrough of the same query shows the index-assisted plan running orders of magnitude faster than the sequential-scan-plus-sort plan it replaces.

### Implementation sketch (Scala)

Geohash encoding and the candidate-then-refine pass, with the interleave made explicit:

```scala
val Base32 = "0123456789bcdefghjkmnpqrstuvwxyz"

def encode(lat: Double, lng: Double, precision: Int): String =
  val sb = StringBuilder()
  var latLo = -90.0; var latHi = 90.0
  var lngLo = -180.0; var lngHi = 180.0
  var ch, bits = 0
  var lngTurn = true                       // longitude bit first
  while sb.length < precision do
    if lngTurn then
      val mid = (lngLo + lngHi) / 2
      if lng >= mid then { ch = (ch << 1) | 1; lngLo = mid } else { ch = ch << 1; lngHi = mid }
    else
      val mid = (latLo + latHi) / 2
      if lat >= mid then { ch = (ch << 1) | 1; latLo = mid } else { ch = ch << 1; latHi = mid }
    lngTurn = !lngTurn
    bits += 1
    if bits == 5 then { sb += Base32(ch); bits = 0; ch = 0 }   // one char per 5 bits
  sb.result()

/** Phase 2 and 3: over-fetch by prefix, then filter by exact distance. */
def nearby(cells: Set[String], q: (Double, Double), radiusM: Double,
           byPrefix: String => Vector[(String, Double, Double)]): Vector[(String, Double)] =
  cells.iterator
    .flatMap(byPrefix)                     // union of the 9 cells; duplicates possible
    .map((id, la, ln) => id -> haversine(q._1, q._2, la, ln))
    .filter(_._2 <= radiusM)
    .toVector.distinctBy(_._1).sortBy(_._2)
```

`haversine` is the standard great-circle distance in metres; `byPrefix` stands for whatever the storage layer offers — a `LIKE 'prefix%'` range scan, a Redis sorted-set slice, or an S2 identifier range.

## Pitfalls

- Querying only the caller's own geohash cell returns plausible results that omit the true nearest point when that point sits across a cell edge; the interleaved encoding lets a metres-wide step flip a high-order bit.
- Treating cell membership as the answer skips phase 3: a geohash cell is a rectangle and the query region is a circle, so the candidate set always contains points outside the radius.
- Choosing precision independently of the radius breaks both directions — cells much smaller than the radius require scanning far more than 9 of them, cells much larger inflate the candidate set the exact-distance pass must process.
- Filtering with a fixed degree offset on longitude produces a region that shrinks toward the poles, because a degree of longitude spans about 111 km at the equator and less elsewhere.
- Deduplication is required after unioning neighbouring cells or overlapping S2 ranges; without it the same member can appear more than once in the candidate list.
- H3's 12 pentagons have five neighbours rather than six, so code that assumes a fixed neighbour count when walking `gridDisk` rings mishandles those cells.
- A quadtree's adaptivity is per-instance state: the leaf boundaries depend on insertion history, so two replicas built from different orderings do not partition identically, unlike a prefix key that is a pure function of the coordinates.
