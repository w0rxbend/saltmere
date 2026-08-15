---
title: "Scaling Memcache at Facebook: a pattern catalog disguised as a paper"
date: 2026-08-15
track: sys-patterns
summary: "Facebook's NSDI '13 paper describes how a fleet of memcached servers handled over a billion requests per second. Its durable contribution is a pattern catalog: leases against stale sets and thundering herds, gutter pools for failover, mcsqueal for invalidation fan-out, and remote markers for cross-region reads. Most of it transfers to systems orders of magnitude smaller."
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

**Gist.** A demand-filled look-aside cache in front of a database admits two races that no amount of client discipline removes: a reader can write a value the database has already superseded, and a deleted hot key can direct every concurrent reader at the database simultaneously. The 2013 NSDI paper *Scaling Memcache at Facebook* fixes both inside the cache server with **leases** — per-key tokens issued on miss, validated on fill, and rate-limited to one every **10 seconds** — and surrounds that core with dedicated failover capacity, commit-log-driven invalidation, and per-key routing markers. The cost is that the cache protocol stops being a two-call `get`/`set` contract: clients must handle token rejection, hold-off responses and retries, and the invalidation path becomes a piece of standing infrastructure that has to be operated.

The paper distinguishes *memcached* — the single-machine, in-memory hash table — from *memcache*, the distributed system built from many such servers. Facebook modified the server where a mechanism demanded it (leases are a server-side change) and left it otherwise intact. The result served **over a billion requests per second** across trillions of items. The absolute capacity figures are historical; the failure modes are not. Each mechanism below exists because a plain look-aside cache breaks in a specific, reproducible way.

## The baseline protocol and its two races

Memcache is used as a *demand-filled look-aside* cache. On read, the client consults the cache; on a miss it fetches from MySQL and issues `set` to install the result. On write, the client updates the database and then issues **`delete`, not `set`** — deletes are idempotent, so concurrent or duplicated invalidations converge, and the next reader repopulates.

Two interleavings defeat that protocol.

**Stale set.** Reader A misses and reads value *v1* from the database. Before A's `set` lands, a writer updates the row to *v2* and deletes the key. A then installs *v1*. The cache now holds a value the database has already superseded, and **no future event will invalidate it**: the delete that would have corrected it has already been consumed. The staleness is bounded only by the key's time-to-live (TTL) or by the next unrelated write.

**Thundering herd.** A frequently read key is deleted. Every concurrent reader misses, and every one of them issues the same database query. Load on the backing store is proportional to concurrent readers rather than to the miss. The general form of this problem and its client-side remedies are covered in [cache stampede and request coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing); this paper's contribution is placing the fix **in the cache server**, where it holds across all clients without coordination between them.

## Leases

On a miss, the memcached server returns a **lease token** bound to that key. The client presents the token with its subsequent `set`; the server accepts the fill only if the token is still valid. **A `delete` on the key voids outstanding tokens**, so the stale-set interleaving is rejected at the server: reader A's fill of *v1* fails because the writer's delete already invalidated A's token. The invariant is that a value may only be installed by a reader whose read of the database is not known to predate an intervening invalidation. Structurally this is the same construction as [fencing tokens](/articles/sys-patterns/2026-08-11-distributed-locking-fencing-tokens) — validation at the resource rather than trust in the client.

The herd is handled by the same token, rate-limited. The server issues a token for a given key at most **once every 10 seconds**. Readers that miss inside that window receive a hold-off response instructing them to retry rather than a token. In the common case the token holder has repopulated the key within a few milliseconds, so the retry is a cache hit. The paper reports a workload whose database peak fell from **17K queries/s without leases to 1.3K queries/s with them**.

The paper also notes a variant for clients that tolerate bounded staleness: rather than blocking, a reader in the hold-off window may be served the recently deleted value marked as stale, exchanging freshness for backend load.

### Implementation sketch (Scala)

The load-bearing part is the client state machine, not the storage. A miss has three outcomes, and only one of them permits a database read.

```scala
enum GetResult:
  case Hit(value: String)
  case Miss(token: Long)   // this client is elected to refill
  case HoldOff             // another client holds the lease; retry

trait LeaseCache:
  def leaseGet(key: String): GetResult
  /** Installs `value` only if `token` has not been voided by a delete. */
  def leaseSet(key: String, value: String, token: Long): Boolean
  def delete(key: String): Unit

def read(cache: LeaseCache, db: String => String, key: String): String =
  cache.leaseGet(key) match
    case GetResult.Hit(v) => v
    case GetResult.HoldOff =>
      Thread.sleep(5)              // the token holder usually refills within ms
      read(cache, db, key)
    case GetResult.Miss(token) =>
      val v = db(key)
      // A `false` here means a delete raced this read; the value is still correct to
      // return to this caller, but the fill must not be re-attempted.
      cache.leaseSet(key, v, token)
      v

def write(cache: LeaseCache, db: (String, String) => Unit,
          key: String, value: String): Unit =
  db(key, value)
  cache.delete(key)                // voids outstanding tokens for `key`
```

The recursion in the `HoldOff` branch is unbounded as written; a production client needs a retry cap, since a permanently failing token holder otherwise leaves readers spinning.

## Gutter: failover without rehashing

When a memcached server stops responding, redistributing its keys onto the surviving servers by consistent hashing risks cascade: a hot key relocated onto a loaded neighbour can take that neighbour down as well. The paper instead reserves a dedicated **gutter pool of roughly 1% of the memcached servers in a cluster**, idle in normal operation. Clients whose requests time out retry against gutter, which caches values with a **short TTL**. The displaced load lands on machines provisioned for exactly that, and the database sees a bounded miss volume rather than a redistribution avalanche. The transferable form: failover capacity is dedicated and stateless-by-design, not borrowed from the healthy serving path.

## Invalidation as infrastructure

With many frontend clusters caching the same rows, delete-on-write is a fan-out problem. Leaving the fan-out to web servers is fragile: a crashed server loses its pending invalidations permanently, and the resulting staleness is silent. Facebook moved invalidation *behind* the database. A daemon named **mcsqueal** tails the MySQL commit log on each database server, extracts cache keys embedded in committed SQL statements, and broadcasts deletes to every frontend cluster in the region, batching them through a layer of **mcrouter** proxies. Mcrouter, since open-sourced, also handles connection pooling and routing. The client transport is split by operation: **`get` requests travel over UDP**, while **`set` and `delete` travel over TCP through mcrouter**, so that loss-sensitive mutations keep a reliable channel while reads accept datagram loss as a miss.

The property that matters is the source of truth: because invalidations are derived from the commit log, **a delete that fails to reach a cluster can be re-derived from the log** rather than being lost with the process that queued it. This is change-data-capture-driven cache invalidation.

## Cold start and cross-region reads

**Cold cluster warmup.** A newly provisioned cluster has a 0% hit rate; directing its misses at the database would overwhelm it. Misses in a cold cluster are instead served from a *warm* cluster's cache. That reintroduces a race — a client can fill the cold cluster with a value invalidated in the interim — which is closed by a blunt rule: a delete against a cold cluster imposes a **two-second hold-off** on refilling that key.

**Remote markers.** In the master-replica multi-region deployment, a user in a replica region who performs a write can read a stale value while MySQL replication catches up. The mechanism: on write, set a **remote marker `rk`** in the regional cache, write to the master database, then delete the local key `k`. A later miss on `k` checks for `rk`; if the marker is present the read is routed to the master region, otherwise the local replica is treated as current. Cross-region read latency is therefore paid only for recently written keys, and only until the marker is cleared.

## Applicability

| Problem | Mechanism | Applies when |
|---|---|---|
| Stale set race | Lease token validated on `set` | any look-aside cache with concurrent writers |
| Thundering herd | Rate-limited lease issuance | hot keys with expensive recompute |
| Server death cascade | Gutter pool (~1%, short TTL) | consistent hashing would overload neighbours |
| Invalidation fan-out | Tail the commit log (mcsqueal) | multiple caches or clusters shadow one database |
| Cold cache load on the database | Proxy misses to a warm peer | bringing up new regions or clusters |
| Read-after-write across regions | Remote marker per key | asynchronous replication with user-visible writes |

The authors state two design goals outright, and both explain the shape of the catalog. First, a change must address a user-facing or operational issue; narrowly scoped optimisations are not pursued. Second, **the probability of reading transient stale data is treated as a tunable parameter** — the system will expose slightly stale data in exchange for insulating the backing store from load. Gutter's short TTL, the stale-marked read during a lease hold-off, and the cold-cluster hold-off are all instances of that second goal rather than separate compromises. The paper also notes that keeping the cache separate from persistent storage lets the two be scaled independently.

## Pitfalls

- **Updating the cache on write instead of deleting it.** Two concurrent writers can install their values in the opposite order to their database commits, leaving the cache permanently disagreeing with the row; deletes avoid this because they commute.
- **Treating a rejected `lease_set` as a transient error and retrying the fill.** The rejection means a delete voided the token, so a retry with the same value reinstalls precisely the stale value the lease was designed to reject.
- **Unbounded retry on hold-off responses.** If the token holder dies before filling the key, every waiting reader spins until the issuance window expires; without a retry cap the symptom is a latency cliff on a single key rather than an error.
- **Failing over onto the surviving servers by rehash.** A relocated hot key concentrates its load on one neighbour, and the observed failure is sequential server deaths rather than a single one — the condition the gutter pool exists to avoid.
- **Broadcasting invalidations from application servers.** A server that crashes with queued deletes loses them silently; the resulting stale keys persist until their TTL, with no log from which to replay the invalidation.
- **Filling a cold cluster from a warm peer without the hold-off.** A value read from the warm cluster immediately before an invalidation is installed into the cold cluster after it, and the cold cluster serves stale data even though the delete was delivered correctly.
- **Assuming the remote marker makes cross-region reads consistent.** It routes reads to the master only while the marker exists; once the marker is removed, a replica still lagging returns the pre-write value.
