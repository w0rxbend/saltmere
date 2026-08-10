---
title: "Cache Invalidation Strategies: The Full Menu, From TTL to CDC"
date: 2026-08-10
track: microservices
summary: "Phil Karlton said cache invalidation is one of the two hard things. Here is the whole toolbox — absolute vs sliding TTL, delete-on-write, versioned/generational keys, write-through, event-driven purge via CDC or pub/sub, CDN surrogate keys, and leases — with the staleness-vs-load trade-offs and the race that makes 'just delete the key' unsafe."
reading_time: 7
tags:
  - caching
  - invalidation
  - redis
  - cdn
  - cdc
  - microservices
sources:
  - title: "Working with surrogate keys | Fastly Documentation"
    url: "https://www.fastly.com/documentation/guides/full-site-delivery/purging/working-with-surrogate-keys/"
  - title: "Client-side caching reference | Redis Docs"
    url: "https://redis.io/docs/latest/develop/reference/client-side-caching/"
  - title: "Cache Consistency: Strategies to Keep Data Fresh | Redis Blog"
    url: "https://redis.io/blog/cache-consistency-strategies/"
  - title: "Debezium connector for PostgreSQL (reference docs)"
    url: "https://debezium.io/documentation/reference/stable/connectors/postgresql.html"
  - title: "Scaling Memcache at Facebook — leases FAQ (MIT 6.824)"
    url: "https://pdos.csail.mit.edu/6.824/papers/memcache-faq.txt"
---

There are only two hard things in computer science, the joke goes, and one of them is cache invalidation. The reason it stays hard is that it is not one problem. It is a family of them, each with a different answer, and the answer depends on how much staleness you can tolerate versus how much load you can afford. This article lays out the full menu. Two adjacent problems have their own articles: keeping the cache and the database consistent under a **dual-write** is covered separately, and the **cache stampede** — the thundering herd that hits your backend the instant a hot key expires — is at [/articles/microservices/2026-08-10-cache-stampede-request-coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing). Here we focus on the act of invalidation itself.

## TTL: absolute vs sliding

The cheapest invalidation is no invalidation at all — you attach a time-to-live and let the entry expire. **Absolute TTL** sets a fixed lifetime from the moment of write: `SET key value EX 300` and the entry dies 300 seconds later no matter how often it is read. **Sliding TTL** resets the clock on every access, so hot data lives forever and cold data ages out. Sliding is great for session data; it is a poor fit for anything with an external source of truth, because a frequently-read key can outlive the underlying data indefinitely.

The trade-off with TTL is baked in. As the Redis team puts it, "a cache relying on TTL alone serves stale data for the entire remaining window after the database changes." A five-minute TTL on a price that changes ten seconds in means readers see the wrong price for four minutes and fifty seconds. Shorten the TTL and staleness drops but miss rate — and backend load — climbs. There is no setting that gives you both, which is why TTL is almost always a *backstop* underneath a more precise mechanism, not the whole strategy.

One subtlety worth internalizing: expiry is not instantaneous, and synchronized expiry is dangerous. If ten thousand keys were all written in the same batch with the same TTL, they all expire in the same second and you get a self-inflicted stampede. Add **jitter** — a random spread on each TTL — so expirations desynchronize:

```python
import random

def cache_ttl(base_seconds: int, jitter: float = 0.15) -> int:
    # +/- 15% so a batch of writes does not expire in lockstep
    delta = int(base_seconds * jitter)
    return base_seconds + random.randint(-delta, delta)
```

The same jitter trick applies to **negative caching** — caching the fact that something does *not* exist, to absorb lookups for missing keys. Give negative entries a short, jittered TTL so a wave of misses for the same absent key does not all refresh together.

## Delete-on-write, and the race that ruins it

The obvious upgrade to TTL is explicit invalidation: when you write the database, delete the cache key so the next read repopulates it. This is the cache-aside write path, and it is correct most of the time. The trouble is a race between a concurrent reader and the writer:

1. Reader gets a cache miss and reads the *old* value from the database.
2. Writer updates the database to the new value and deletes the cache key.
3. Reader — still holding the old value — writes it back into the cache.

Now the cache holds a stale value with a full TTL ahead of it, and no further write will fix it until expiry. The Redis blog calls this a **stale set**: "concurrent updates get reordered, leaving the cache holding a value that doesn't reflect the latest write." The racing window is tiny, but at high fill volumes even rare races show up often enough to matter.

The common mitigation is **delayed double delete**: the writer deletes the key, updates the database, then schedules a *second* delete a short interval later — long enough to cover the slow reader's write-back. It does not make the operation atomic; it shrinks the window of staleness from "one full TTL" to "a few hundred milliseconds." For true correctness you need either leases (below) or versioning.

## Write-through and write-behind

**Write-through** updates the cache and the database together on the write path, so the cache is never behind. The Redis guidance is to reserve it for "data where correctness is the point, like account balances and payments," and it notes the reasonable ergonomics: "users tend to handle write latency better than read latency, so the double-write cost lands in the right place." The costs are real — every write waits on two systems, and a partial failure leaves an inconsistency to reconcile. **Write-behind** (write-back) buffers the write and flushes asynchronously, trading durability for latency; a crash before flush loses data, so it suits metrics and counters more than money.

## Versioned and generational keys

Sometimes you need to invalidate a whole *namespace* at once — every cached fragment for a product, every rendered page for a user — without scanning for keys or issuing a delete per entry. The trick is a **version pointer**. Embed a version number in the key, and store the current version in one small cell:

```python
def product_key(pid: int, version: int) -> str:
    return f"product:{pid}:v{version}:render"

def read_product(pid: int):
    version = r.get(f"product:{pid}:ver") or 0
    key = product_key(pid, int(version))
    if (hit := r.get(key)) is not None:
        return hit
    value = render_from_db(pid)
    r.set(key, value, ex=cache_ttl(3600))
    return value

def invalidate_product(pid: int):
    # One INCR retires every cached key for this product, instantly.
    r.incr(f"product:{pid}:ver")
```

Bumping the version orphans every old key in a single atomic `INCR` — no scan, no `KEYS`, no delete storm. The orphaned entries are never read again and fall off on their own TTL. This is also the cleanest defense against a stale set: even if a slow reader writes back, it writes to the *old* version key, which nobody will ever request. The cost is memory — dead generations linger until they expire — and this is exactly why versioned keys always carry a TTL backstop. The same pattern doubles as a rolling-deploy safety valve: bump a global schema version and every serializer-incompatible entry from the old build is retired at once.

## Event-driven invalidation: pub/sub and CDC

Delete-on-write assumes the writer *is* your service. In a system where many services (or a batch job, or a DBA) can mutate a table, the reliable signal is the database's own change log. **Change data capture** turns committed writes into an event stream: Debezium tails the Postgres WAL (or MySQL binlog) and emits a `before`/`after`/`op` envelope per row change onto Kafka. A small consumer maps those events to cache invalidations, so the cache reacts to *every* write, not just the ones your code made:

```python
# Kafka consumer over a Debezium change topic -> cache invalidation
for msg in consumer:                      # topic: dbserver.public.product
    change = json.loads(msg.value)
    payload = change["payload"]
    op = payload["op"]                     # c=create, u=update, d=delete
    if op in ("u", "d", "c"):
        pid = (payload["after"] or payload["before"])["id"]
        r.incr(f"product:{pid}:ver")       # version bump = namespace invalidation
    consumer.commit()
```

Because CDC reads the log *after commit*, it never fires a phantom invalidation for a rolled-back transaction — the class of bug that plagues naive dual-writes. For same-process or single-cluster caches, **Redis pub/sub** is the lighter-weight cousin. Redis even ships this as a first-class feature: server-assisted client-side caching. With `CLIENT TRACKING ON`, the server remembers which keys a client read and pushes an invalidation when any of them change — over RESP3 push messages, or in RESP2 via a `SUBSCRIBE __redis__:invalidate` channel. `BCAST` mode trades precision for zero server memory by invalidating on key *prefixes*. Notably, the Redis client-side protocol has the same read/invalidate race as delete-on-write, and its documented fix — mark an entry "caching-in-progress," and skip the write-back if an invalidation arrived meanwhile — is the placeholder cousin of the double-delete.

## Tag-based / surrogate-key invalidation at the CDN

At the edge, you cannot enumerate URLs to purge — one article change might touch a hundred rendered pages. CDNs solve this with **surrogate keys** (Fastly's name; "cache tags" elsewhere). The origin attaches a space-separated `Surrogate-Key: veggie seasonal central-mexico` header to each response, and the CDN indexes objects by those tags. One object can carry many keys, one key can tag many objects, and "purging a single key invalidates all objects associated with it" — a single API call retires an arbitrary set. Fastly's **soft purge** marks the tagged objects stale rather than evicting them, so the edge can serve stale-while-revalidate instead of taking a miss storm. Surrogate keys are just versioned/generational invalidation applied to HTTP, with the tag index living in the CDN.

## Leases: serialize the fill

The strongest primitive comes from *Scaling Memcache at Facebook*. On a miss, the cache hands the client a **lease token**; the client must present that token to install the value. Two things then fall out for free. First, stale sets are impossible: if the key is deleted (invalidated) while the client is fetching from the database, the lease is voided and "memcache rejects the install because the lease is invalid," so an in-flight stale read can never overwrite fresher data. Second, the herd is tamed: the server grants a lease "only the first client that misses," and the rest wait briefly and re-read, giving one fetcher a chance to fill. That second property overlaps with the stampede article — leases are one of the coalescing mechanisms discussed there.

## Choosing

Match the mechanism to the tolerance. Pure TTL when bounded staleness is fine and simplicity wins; delete-on-write (plus a double-delete) for single-writer cache-aside; versioned keys when you must retire a namespace atomically or survive rolling deploys; CDC/pub/sub when writers are plural or external; surrogate keys at the CDN; leases when a stale set is genuinely unacceptable. In practice the teams who get this right *layer* it: a precise signal on top, an event-driven refresh in the middle, and a jittered TTL as the backstop that saves you when the precise signal is dropped.

**Try next:** wire a Debezium change topic to the version-bump consumer above, then measure how far your p99 staleness drops compared to TTL alone — and confirm the orphaned key generations really do age out under load.
