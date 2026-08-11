---
title: "Find things near me at scale: geohash, quadtree, and H3"
date: 2026-08-11
track: sys-patterns
summary: "A B-tree can range-scan one column fast, but 'near me' is a 2D question that defeats it. Space-filling curves, quadtrees, and hexagonal grids turn proximity back into a prefix or integer lookup — each with its own trade-offs on uniformity, neighbors, and density."
reading_time: 6
tags: [geospatial, geohash, quadtree, h3, redis, indexing]
sources:
  - title: "Geohash — Wikipedia"
    url: "https://en.wikipedia.org/wiki/Geohash"
  - title: "H3: Uber's Hexagonal Hierarchical Spatial Index"
    url: "https://www.uber.com/us/en/blog/h3/"
  - title: "Redis GEOSEARCH command reference"
    url: "https://redis.io/docs/latest/commands/geosearch/"
  - title: "Redis geospatial data type"
    url: "https://redis.io/docs/latest/develop/data-types/geospatial/"
  - title: "Quadtree — Wikipedia"
    url: "https://en.wikipedia.org/wiki/Quadtree"
---

Every ride-hailing, dating, and delivery app runs the same query a million times a minute: *what is close to this point?* It sounds trivial until you try to serve it from a normal database. The obvious schema — a `drivers` table with `lat` and `lng` columns — invites the obvious index. It does not work.

## Why a B-tree can't answer "near me"

A B-tree indexes an ordered key, and it is superb at range scans on **one** dimension. Ask for `lat BETWEEN 40.70 AND 40.75` and it walks a contiguous slab of the tree. The trouble is that a compound index on `(lat, lng)` sorts by `lat` first and only breaks ties with `lng`. So `lat BETWEEN 40.70 AND 40.75 AND lng BETWEEN -74.02 AND -73.97` can range-scan the latitude bound, but within that band every longitude in the world is interleaved. You scan a horizontal stripe across the entire planet and filter almost all of it away. Two points a meter apart can sit arbitrarily far apart in index order.

The fix in every scheme below is the same idea: collapse two dimensions into **one** ordering where spatial closeness survives, so proximity becomes a range or prefix scan again.

## Geohash: interleave the bits

Geohash recursively bisects the world. Longitude splits the range `[-180, 180]` into east/west; latitude splits `[-90, 90]` into north/south. Each split emits one bit. You **interleave** them — lng, lat, lng, lat, … — group the bit stream into 5-bit chunks, and map each chunk to a base32 character. The result is a short string like `dr5ru`.

The payoff is the prefix property: **the longer the shared prefix, the closer two points are**. `dr5ru7` and `dr5ru8` sit in adjacent cells; truncating to `dr5r` gives you a coarser box that contains both. Precision is just string length — 5 characters is roughly a 5 km box, 7 characters about 150 m.

```python
import geohash  # python-geohash
here = geohash.encode(40.7484, -73.9857, precision=7)  # -> 'dr5ru7z'

# "nearby": everyone sharing my 6-char cell...
prefix = here[:6]  # 'dr5ru7'
# SQL against a geohash column:
#   SELECT id FROM drivers WHERE gh LIKE 'dr5ru7%';
```

Here is the edge-case that bites everyone. Prefix similarity implies proximity, but proximity does **not** imply prefix similarity. Cells straddle bisection boundaries, so two physically adjacent points can differ in the very first character — think of a point right on the equator or a prime-meridian seam. A naive `LIKE 'dr5ru7%'` silently drops neighbors sitting just across a cell edge. The standard remedy: compute the target cell **and its 8 neighbors**, then query all nine.

```python
cells = [here[:6]] + geohash.neighbors(here[:6])  # 9 total
#   WHERE gh LIKE 'dr5ru7%' OR gh LIKE 'dr5ru5%' OR ... (9 prefixes)
```

## Quadtree: subdivide where it's crowded

A geohash grid is uniform — every cell at a given precision is the same size, whether it covers midtown Manhattan or empty ocean. A **quadtree** adapts instead. Start with one square covering the whole space; whenever a node holds more than *k* points, split it into four quadrants (NW, NE, SW, SE) and push the points down. Dense areas subdivide deeply; sparse areas stay shallow.

That density-awareness is the quadtree's whole reason to exist. A city gets a deep, fine-grained tree with a few points per leaf; an ocean stays one coarse node. A proximity query descends to the leaf containing your point and inspects sibling leaves. The cost is that the structure is dynamic — insert-heavy workloads trigger splits and rebalancing, which is fiddlier to shard than a stateless geohash string. Uber's early dispatch system famously used a quadtree before moving on.

## H3: hexagons instead of squares

Square grids have a subtle defect: a cell has 8 neighbors, but 4 share an edge and 4 only touch at a corner, at a different distance. That asymmetry complicates any "expand outward by one ring" logic. Uber's **H3** replaces squares with hexagons. A hexagon has exactly 6 neighbors and — the key property — there is only **one** distance between a cell's center and each neighbor's center. Rings of "cells within k steps" are clean and isotropic.

H3 projects the globe onto an icosahedron and tiles it with hexagons across **16 resolutions (0–15)**, each finer level having cells roughly one-seventh the area of the level above. Every cell is a 64-bit integer, so lookups and joins are integer-fast. Because you can't perfectly tile a sphere with hexagons, H3 includes exactly **12 pentagons** at the icosahedron's vertices — Uber orients them over the oceans so land analysis rarely hits one.

```python
import h3
cell = h3.latlng_to_cell(40.7484, -73.9857, 9)  # res 9 ~ 0.1 km^2
ring = h3.grid_disk(cell, 1)   # the cell + its 6 neighbors
#   SELECT id FROM drivers WHERE h3_9 = ANY(:ring);
```

## Reach for the tools, not the algorithm

You rarely implement these by hand. **Redis** ships geospatial commands backed by a 52-bit geohash stored as a sorted-set score:

```
GEOADD drivers 13.361389 38.115556 "alice"
GEOSEARCH drivers FROMLONLAT 15 37 BYRADIUS 5 km ASC WITHDIST COUNT 20
```

**PostGIS** gives you `geography` columns with GiST (R-tree) indexes and `ST_DWithin` for radius queries; **H3** has bindings for Postgres, Spark, and BigQuery when you need grid-based aggregation.

| Scheme | Cell shape | Uniformity | Neighbor handling | Density-adaptive |
|---|---|---|---|---|
| Geohash | rectangle | uniform per precision | 8 neighbors, seam pitfall | no |
| Quadtree | square | variable by depth | 8, corner asymmetry | yes |
| H3 | hexagon | near-uniform globally | 6, single distance | no |

Pick geohash when you want a dead-simple string you can index in any database. Pick a quadtree when your data is wildly uneven and you want cells to follow the crowd. Pick H3 when clean neighbor rings and uniform aggregation matter — heatmaps, surge zones, coverage analysis.

**Try next:** load 10k random points into Redis with `GEOADD`, then compare a 5 km `GEOSEARCH` against a hand-rolled 9-cell geohash prefix query — and count how many rows each one scans.
