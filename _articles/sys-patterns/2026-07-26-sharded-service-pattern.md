---
title: "The Sharded Service: When Replicas Can't Hold All the State"
date: 2026-07-26
track: sys-patterns
summary: "Replicated services scale by cloning; sharded services scale by dividing. A look at Burns' sharded-service pattern — the sharding function, hot shards, rebalancing on growth, and a sharded cache sitting in front of a scatter-gather root."
reading_time: 5
tags: [sharding, hot-shards, caching, scalability, kubernetes, statefulset, burns]
sources:
  - title: "Designing Distributed Systems, 2nd ed. — Ch. 6, Sharded Services (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781491983638/ch06.html"
  - title: "Design Patterns for Container-based Distributed Systems (Burns & Oppenheimer, USENIX HotCloud '16)"
    url: "https://www.usenix.org/conference/hotcloud16/workshop-program/presentation/burns"
  - title: "Design patterns for container-based distributed systems (Google Research publication page)"
    url: "https://research.google/pubs/design-patterns-for-container-based-distributed-systems/"
  - title: "Sharded Services (Bindu C, Medium)"
    url: "https://medium.com/@bindubc/sharded-services-68db32e03d80"
  - title: "Sharded Services — Introduction to Distributed Systems (Educative)"
    url: "https://www.educative.io/courses/introduction-to-distributed-systems-for-dummies/np/sharded-services"
---

A replicated service scales the boring way: clone the container, put a load balancer in front, and any replica can answer any request because every replica holds the same thing (or nothing). That trick stops working the moment the *thing being served* is bigger than one machine — a cache with more keys than fit in RAM, a user index too large for one disk, a leaderboard too hot for one CPU. Burns' answer in *Designing Distributed Systems* is the **sharded service**: split the state across replicas that are no longer interchangeable, and put a routing node in front that knows which replica owns which slice.

## Root and shards, not load balancer and replicas

In a sharded service, each replica — a **shard** — serves only a subset of requests. A **root** node inspects each incoming request, computes which shard owns it, and forwards accordingly. The root is doing real work, not blind round-robin: get the routing wrong and you either 404 a key that exists or, worse, silently split one shard's writes across two.

| | Replicated service | Sharded service |
|---|---|---|
| Replicas | Identical, stateless or fully synced | Disjoint, each owns a slice of state |
| Router's job | Pick *any* healthy replica | Pick the *one correct* replica |
| Scales with | Request rate | Data volume (and request rate) |
| Failure of one node | Capacity dips | A slice of the keyspace goes dark |

That last row is the tax: sharding buys capacity by trading away the safety net of "any node will do." Each shard typically still needs its own replication underneath to survive a node failure — sharding and replication are orthogonal and usually combined, not either/or.

## The sharding function

The router needs a deterministic, uniform function from request key to shard id. The naive version:

```python
def shard_for_key(key: str, shard_count: int) -> int:
    digest = hashlib.sha1(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") % shard_count
```

Two things matter more than the hash algorithm itself:

- **Determinism and uniformity** — same key always lands on the same shard, and keys spread evenly so no shard is structurally overloaded.
- **Key granularity** — shard on something coarse enough to keep related lookups (and cache locality) together, but fine enough that no single key's traffic can dominate a shard.

The `% shard_count` term is also the function's biggest liability: change `shard_count` and almost every key remaps. That's the problem consistent hashing exists to solve — bounding remaps to roughly `keys / shards` instead of nearly all of them — and it's covered in depth in this journal's dedicated hash-ring article, so I won't re-derive it here. The point for the sharded-service pattern is narrower: whatever hashing scheme you pick, plan for resizing from day one, because you will resize.

## Hot shards

A perfectly uniform hash over keys does not guarantee uniform *load*, because real traffic isn't uniform — one viral post, one celebrity account, one popular SKU can pin a single shard's CPU or network while its siblings idle. This is the **hot shard** problem, and it's structural to sharding in a way replicated services never face, because a replicated service would have just spread that same hot key across every replica.

Mitigations, roughly in order of how much they change your architecture:

1. **Split the hot key further** — sub-shard by a secondary dimension (time bucket, region) so no single shard absorbs it alone.
2. **Replicate the hot shard, not the whole tier** — give the hot shard extra read replicas while cold shards stay single-instance. This is sharding and replication composed deliberately, not uniformly.
3. **Move, don't just add** — relocate a hot shard onto its own dedicated node while colder shards keep sharing hardware, rebalancing physical placement without touching the sharding function at all.

Detecting a hot shard is a monitoring problem before it's an architecture problem: per-shard p99 latency and per-shard QPS, not just tier-wide averages, or the hot shard hides inside a healthy-looking mean.

## Adding shards and rebalancing

Growth means changing shard count, which means some keys must move. The operational sequence that keeps this safe:

1. Stand up the new shard(s) empty.
2. Update the sharding function (or the consistent-hash ring) so newly written keys route correctly immediately.
3. Migrate existing keys lazily: on a read that misses the new shard, fall back to the old owner, fetch, and backfill the new one.
4. Once a background sweep confirms a key range fully migrated, stop routing reads to the old shard for that range.

That lazy-migrate-on-miss step is what makes rebalancing survivable in production — it turns a big-bang cutover into a rolling one, at the cost of a temporary double-lookup path in the root.

## A sharded cache in front of a service

The clearest concrete instance of this pattern is a cache tier sharded in front of an origin service, with the root doing double duty as a scatter-gather coordinator for multi-key requests:

```python
SHARD_CLIENTS = [redis.Redis(host=f"cache-shard-{i}.cache-shard") for i in range(4)]

def get_or_fetch(key: str, origin):
    shard = SHARD_CLIENTS[shard_for_key(key, len(SHARD_CLIENTS))]
    cached = shard.get(key)
    if cached is not None:
        return cached
    value = origin.fetch(key)          # cache miss -> hit the origin service
    shard.set(value, ex=300)
    return value

def scatter_gather(keys: list[str]) -> dict:
    """A multi-key request touches several shards; the root fans out
    and gathers. Latency is bounded by the SLOWEST shard, not the average —
    the classic scatter-gather tax."""
    by_shard: dict[int, list[str]] = {}
    for k in keys:
        by_shard.setdefault(shard_for_key(k, len(SHARD_CLIENTS)), []).append(k)
    results = {}
    for shard_id, shard_keys in by_shard.items():
        results.update(SHARD_CLIENTS[shard_id].mget(shard_keys))
    return results
```

Each shard can be a plain `StatefulSet` so ordinals give stable, addressable identity that the sharding function can map directly to a pod:

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: cache-shard
spec:
  serviceName: cache-shard
  replicas: 4
  selector: { matchLabels: { app: cache-shard } }
  template:
    metadata: { labels: { app: cache-shard } }
    spec:
      containers:
        - name: redis
          image: redis:7
          ports: [{ containerPort: 6379 }]
```

`cache-shard-0.cache-shard` through `cache-shard-3.cache-shard` are stable DNS names the root can hash directly into. Growing capacity is `replicas: 4` → `replicas: 6`, then running the rebalance sequence above — the StatefulSet gives you the new pods; it does not migrate a single key for you.

**Try next:** stand up the four-pod `StatefulSet` above, populate it through `get_or_fetch`, then bump `replicas` to 6 and update `shard_for_key`'s modulus without a migration step — watch your hit rate collapse as most keys silently point at the wrong shard, and feel why the lazy-migrate-on-miss step isn't optional.
