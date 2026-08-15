---
title: "Scaling Memcache at Facebook: a pattern catalog disguised as a paper"
date: 2026-08-15
track: sys-patterns
summary: "Facebook's NSDI '13 paper describes how a fleet of vanilla memcached servers handled over a billion requests per second. The real value is the pattern catalog: leases against stale sets and thundering herds, gutter pools for failover, mcsqueal for invalidation fan-out, and remote markers for cross-region reads. Almost all of it transfers to systems a thousand times smaller."
reading_time: 6
tags: [caching, memcached, facebook, leases, invalidation, multi-region]
sources:
  - title: "Nishtala et al. — Scaling Memcache at Facebook (NSDI '13, paper PDF)"
    url: "https://www.usenix.org/system/files/conference/nsdi13/nsdi13-final170_update.pdf"
  - title: "USENIX NSDI '13 — presentation page (slides + video)"
    url: "https://www.usenix.org/conference/nsdi13/technical-sessions/presentation/nishtala"
  - title: "Micah Lerner — Scaling Memcache at Facebook (paper walkthrough)"
    url: "https://www.micahlerner.com/2021/05/31/scaling-memcache-at-facebook.html"
  - title: "MIT 6.5840 — Scaling Memcache at Facebook lecture notes"
    url: "https://pdos.lcs.mit.edu/6.824/notes/l-memcached.txt"
  - title: "facebook/mcrouter — wiki (the open-sourced routing layer)"
    url: "https://github.com/facebook/mcrouter/wiki/Home"
---

The 2013 NSDI paper *Scaling Memcache at Facebook* describes how Facebook turned stock memcached — a single-machine, in-memory hash table — into a globally distributed cache handling **over a billion requests per second** across trillions of items. The architecture numbers are museum pieces now; the failure modes are not. Every mechanism in the paper exists because a plain look-aside cache breaks in a specific way at scale, and each fix is a reusable pattern. This is a tour of the breakages and what to steal.

## The baseline: look-aside caching and its two bugs

Facebook used memcache as a *demand-filled look-aside* cache: on read, try the cache, and on a miss fetch from MySQL and `set` the result back. On write, update the database and then **delete** (not update) the cached key — deletes are idempotent, and the next reader repopulates.

Two bugs hide in that innocent protocol:

1. **Stale sets.** Reader A misses, fetches value v1 from the DB. Meanwhile a writer updates the row to v2 and deletes the key. A, unaware, now does `set(k, v1)` — and the cache holds stale data *indefinitely*, because nothing will ever invalidate it again.
2. **Thundering herd.** A hot key gets deleted; thousands of concurrent readers all miss and all hammer the database at once. The general problem and app-level fixes are covered in [cache stampede and request coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing) — this paper's contribution is fixing it *in the cache server*.

## Leases: one mechanism, both bugs

On a miss, the memcached server hands the client a **lease token** bound to that key. The client passes the token back with its `set`, and the server verifies it — a delete arriving in between invalidates the token, so the stale set bounces. If the shape sounds familiar, it should: it is the same trick as [fencing tokens](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens), enforced at the resource rather than trusted at the client.

The herd fix is rate-limiting token issuance: the server returns a token for a given key only **once every 10 seconds**. Other clients that miss in that window get a "hold off and retry" response, and typically the winner has repopulated the key by their retry a few milliseconds later — so they hit the cache instead of the database. The paper's numbers: a workload with a peak of **17K DB queries/s** without leases dropped to **1.3K/s** with them.

```text
# lease-based look-aside (client side)
def get(k):
    v, lease = cache.get(k)          # miss returns a lease token (or "try later")
    if v is not None: return v
    if lease is None:                 # someone else holds the lease
        sleep(short); return get(k)   # retry; usually hits the refilled cache
    v = db.query(k)
    cache.lease_set(k, v, lease)      # server rejects if a delete voided the token
    return v

def write(k, v):
    db.update(k, v)
    cache.delete(k)                   # voids outstanding leases for k
```

A bonus: for users who can tolerate it, a delete can leave the old value in limbo briefly and serve it as a marked *stale* value instead of blocking — trading freshness for load.

## Gutter: failover without rehashing

When a memcached server dies, consistent-hashing its keys onto the survivors is dangerous: a hot key can turn the next server into the next casualty, cascading. Facebook instead reserves a small **gutter pool** — about **1% of the fleet** — that idles until a server stops responding. Clients that time out retry against gutter, which caches the value with a short TTL. The load lands on machines whose whole job is absorbing it, and the database sees a bounded miss storm rather than a redistribution avalanche. Pattern to steal: *failover capacity should be dedicated and dumb, not borrowed from the healthy path.*

## Invalidation as infrastructure: mcsqueal and mcrouter

With many frontend clusters caching the same data, "delete on write" becomes a fan-out problem. Relying on web servers to broadcast deletes is fragile — a crashed server loses its pending invalidations forever. Facebook moved invalidation *behind* the database: a daemon called **mcsqueal** tails the MySQL commit log on every DB server, extracts the cache keys embedded in committed SQL statements, and broadcasts deletes to every frontend cluster in the region, batching them through a layer of **mcrouter** proxies (open-sourced; it also handles connection pooling, routing, and the UDP-for-gets/TCP-for-sets split). The commit log is the source of truth, so invalidations can be replayed after a failure. This is CDC-driven cache invalidation, a decade before Debezium made it fashionable.

## Cold start and cross-region reads

Two more patterns round out the catalog:

**Cold cluster warmup.** A freshly provisioned cluster has a 0% hit rate; pointing it at the database would melt it. Instead, cold-cluster misses are served from a *warm* cluster's cache. The race (client fills cold cluster with a value that was invalidated in between) is closed crudely: deletes to the cold cluster impose a **two-second hold-off** on re-filling that key. Not elegant — effective.

**Remote markers.** In the master-replica multi-region setup, a user in a replica region who writes data may read stale results while MySQL replication catches up. Fix: on write, set a **remote marker** `rk` in the regional cache, write to the master DB, delete the local key. A later miss on `k` checks for `rk`: if present, the read is routed to the master region; if not, the local replica is fresh enough. You pay cross-region latency only for recently-written keys.

## What to steal

| Problem | Mechanism | Steal it when... |
|---|---|---|
| Stale set race | Lease token validated on `set` | any look-aside cache with concurrent writers |
| Thundering herd | Rate-limited lease issuance | hot keys + expensive recompute |
| Server death cascade | Gutter pool (1%, short TTL) | consistent hashing would overload neighbors |
| Invalidation fan-out | Tail the commit log (mcsqueal) | multiple caches/clusters shadow one DB |
| Cold cache melts DB | Proxy misses to a warm peer | bringing up new regions/clusters |
| Read-your-writes cross-region | Remote marker per key | async replication + user-visible writes |

The meta-lesson the authors state outright: they push complexity **into the client and the surrounding infrastructure**, keeping memcached itself a dumb, fast hash table. Separating cache policy from cache storage is what let every one of these patterns evolve independently.

**Try next:** implement `lease_get`/`lease_set` over Redis (`SET k-lease token NX PX 10000`, validate token in a Lua script on fill) and race 100 readers against a writer; without the lease check you can reliably reproduce a permanently stale key.
