---
title: "Cache Warming and Preloading: Surviving the Cold Start"
date: 2026-08-10
track: sys-patterns
summary: "A fresh, flushed, or failed-over cache serves everything as a miss, and that flood of misses can knock over the origin the cache was meant to protect. This article covers the cold-cache avalanche, then five warming strategies — deploy/startup warm-up, refresh-ahead, staged traffic ramp, shadow/dual-cache warming, and working-set capture — with code for seeding top-N keys and a Caffeine refresh-ahead loader. Ties warming to failover and autoscaling, where every new node starts cold."
reading_time: 7
tags: [caching, cache-warming, preloading, cold-start, refresh-ahead, failover, autoscaling, interview-prep]
sources:
  - title: "Caffeine Wiki — Refresh (refreshAfterWrite, CacheLoader.reload)"
    url: "https://github.com/ben-manes/caffeine/wiki/Refresh"
  - title: "Netflix TechBlog — Cache Warming: Agility for a Stateful Service (EVCache)"
    url: "https://netflixtechblog.com/cache-warming-agility-for-a-stateful-service-2d3b1da82642"
  - title: "Amazon ElastiCache — Caching strategies for Memcached (lazy loading, write-through, TTL)"
    url: "https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/Strategies.html"
  - title: "Amazon ElastiCache — Managing Reserved Memory for Valkey and Redis OSS"
    url: "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/redis-memory-management.html"
  - title: "Aerospike — Cache Warming Explained: Benefits, Pitfalls, and Alternatives"
    url: "https://aerospike.com/blog/cache-warming-explained/"
---

**Gist.** A cache absorbs reads the origin would otherwise serve, but only while it holds the working set; immediately after a deploy, a flush, a failover, or a scale-out, every request is a miss and the full read load lands on the origin. Warming and preloading populate the cache before or alongside the traffic that would otherwise miss, using pre-seeded hot keys, background refresh, metered traffic ramps, or a copy of another node's contents. The cost is a second load path that must itself be rate-limited, plus the memory, complexity, and staleness the warm data introduces.

## The arithmetic of a cold cache

Let *h* be the steady-state hit ratio. In steady state the origin sees a fraction 1 − *h* of read traffic; with an empty cache it sees all of it. The multiplier on origin read load during the cold window is therefore **1/(1 − h)**: at *h* = 0.95 the origin instantaneously absorbs roughly **20× its normal read rate**. That surge is a self-inflicted [cache avalanche](/articles/distributed-systems/2026-08-10-cache-penetration-breakdown-avalanche) — the layer meant to shield the database becomes the reason it fails.

The hazard is that the multiplier applies at correlated moments. Caches go cold when something else has already gone wrong: a node died, a region flipped, a scale-out fired under a spike. AWS's lazy-loading guidance states the mechanism directly: when a node fails and is replaced by a new, empty node, the application keeps functioning with increased latency, and "each cache miss results in a query of the database". The latency increase is the visible symptom; the load multiplier is the cause.

## When warming is worth its complexity

The default is **lazy loading** (cache-aside): entries are populated on miss. It caches only data that was requested, and for a cache that comes up during a quiet window and fills gradually, no further mechanism is needed.

Warming earns its complexity when **the cold window overlaps real load** and the origin cannot absorb the miss flood. The conditions that make it pay:

- The origin is expensive or fragile relative to peak queries per second (QPS) — a single primary database, a slow downstream, a machine-learning model.
- The working set is **skewed**, so a small hot set serves most traffic and a small warm buys most of the benefit.
- Cold events are scheduled or predictable: deploys, blue/green cutovers, autoscaling, failover drills.
- Recompute cost per entry is high (rendered pages, aggregations, embeddings).

Warming does not pay when the keyspace is large and flat — there is no hot set to prioritise — when misses are cheap, or when the cache fills faster than traffic ramps.

## Strategy 1: proactive warm-up on deploy or startup

Before a node accepts traffic, the **top-N hot keys** are pushed into it. The key list comes from an existing source: access logs, an analytics rollup, a hot-key sample, or a dump of currently hot keys from a sibling node. **Readiness is then gated on warm-up completion**, so the load balancer does not route to a node that is still cold. Without that gate the warm-up and the live traffic contend for the same origin, and the node reports healthy while its hit ratio is near zero.

Two details separate a working warm-up from an outage. First, **the warm-up must throttle itself**: a loop that issues N thousand parallel loads is the avalanche it was written to prevent, so both the concurrency and the origin request rate must be bounded. Second, **candidates are prioritised by value rather than recency** — ordering by request frequency multiplied by recompute cost, warming the leading slice, and stopping when the marginal hit-ratio gain flattens. Where the distribution is skewed, warming a small leading percentage of keys recovers the bulk of the hit ratio.

### Implementation sketch (Scala)

```scala
// Seed the top-N hot keys, bounding both concurrency and origin request rate,
// and only then flip readiness. hotKeys is ordered by request count descending.
trait Cache[K, V]:
  def getIfPresent(key: K): Option[V]
  def put(key: K, value: V): Unit

final class Warmer[K, V](
    cache: Cache[K, V],
    origin: K => Option[V],
    maxConcurrent: Int,
    minGapNanos: Long        // 1e9 / target origin QPS
):
  private val slots = Semaphore(maxConcurrent)
  private val nextSlot = AtomicLong(System.nanoTime())

  private def pace(): Unit =
    // Each caller reserves a distinct departure instant, so successive origin
    // reads are separated by at least minGapNanos regardless of thread count.
    val due = nextSlot.getAndAdd(minGapNanos)
    val wait = due - System.nanoTime()
    if wait > 0 then LockSupport.parkNanos(wait)

  def warm(hotKeys: Seq[K], n: Int)(using ExecutionContext): Future[Unit] =
    val work = hotKeys.take(n).map { key =>
      Future {
        slots.acquire()
        try
          if cache.getIfPresent(key).isEmpty then
            pace()
            origin(key).foreach(cache.put(key, _))   // pre-pay the expensive read
        finally slots.release()
      }.recover { case _: Exception => () }          // one failed key must not abort the warm
    }
    Future.sequence(work).map(_ => ())
```

The `recover` is load-bearing: a warm-up that propagates the first failure leaves the node cold and, if readiness is gated on it, permanently out of rotation.

## Strategy 2: refresh-ahead, so hot entries do not expire under load

Warming addresses the initial cold cache. Refresh-ahead addresses a narrower cold spot: a hot key whose time-to-live (TTL) expires at peak, forcing a synchronous miss and, where many keys share an expiry instant, a [stampede](/articles/microservices/2026-08-10-cache-stampede-request-coalescing). The mechanism refreshes popular entries in the background before they become stale.

Caffeine implements this with `refreshAfterWrite`. Per the Caffeine documentation, it makes a key eligible for refresh after the specified duration, but a refresh is initiated only when the entry is queried — and the refresh is **asynchronous**: the previous cached value continues to be served while the new value loads. **No reader blocks on a reload.**

```java
LoadingCache<Key, Graph> graphs = Caffeine.newBuilder()
    .maximumSize(10_000)
    .refreshAfterWrite(Duration.ofMinutes(1))   // eligible to refresh after 1 min...
    .expireAfterWrite(Duration.ofMinutes(5))    // ...but only truly expires at 5 min
    .build(key -> loadExpensiveGraph(key));      // CacheLoader for load AND async refresh
```

Pairing a short `refreshAfterWrite` with a longer `expireAfterWrite` separates the two populations: hot keys, being queried, are refreshed silently and never expire under load; cold keys, never queried, are never refreshed and hard-expire, releasing memory. Overriding `CacheLoader.reload(key, oldValue)` lets the refresh reuse the previous value — for batched reloads, or to serve the stale value on error, since Caffeine retains the old value if the reload throws. The pattern applies where recompute is expensive and a few seconds of staleness is acceptable; see also the [admission-control discussion](/articles/sys-patterns/2026-08-10-tinylfu-cache-admission-control/).

## Strategy 3: staged traffic ramp after failover

A freshly started node need not receive its full share immediately. **Traffic is ramped** — a small percentage, then a larger one, over tens of seconds — so the node fills its working set from real reads while the origin sees only a fraction of the miss load at any instant. This is the dual of pre-filling: instead of loading before traffic arrives, the traffic is metered to the fill rate. Load balancers offering slow start, which weight newly healthy targets upward over an interval, implement this, and it composes with autoscaling, where scale-out adds cold nodes mid-spike.

## Strategy 4: shadow or dual-cache warming before cutover

For cache migrations and version upgrades, the **new cache runs in shadow**: production reads (and writes, where applicable) are mirrored into it so it fills with the live working set before cutover. Traffic is flipped only once its hit ratio matches the incumbent's. This avoids promoting an empty cluster into the request path at full load. The cost is two caches running in parallel for the duration of the shadow window.

## Strategy 5: capturing the working set

Copying the actual working set is more reliable than predicting it. Netflix's EVCache enumerates keys on source nodes via the memcached LRU crawler, dumps values to S3 in chunks, and repopulates the target through a pipeline decoupled by SQS rather than replaying traffic. The reported figures bound what the technique handles: a replaced instance "warmed up in less than 15 minutes with about 2.2 GB"; a 12 TB, 500-million-item replica in roughly 2 hours; a largest run of 700 TB across 380 nodes. The general form — mirror production reads into the new cache, or dump and restore the hot set — **produces a warm cache without touching the origin**, which matters precisely when the origin is the fragile component.

## Failover and autoscaling: every new node starts cold

The two most common sources of cold caches are failover and autoscaling. Both are automatic, so the warming must be automatic as well. A Multi-AZ failover promotes a replica, which is warm if it was replicating and cold if it was freshly built. A scale-out adds nodes because load is already high, so cold nodes join under peak. The lifecycle hooks are the three preceding strategies: gate the readiness or health check on warm-up completion, keep hot entries fresh with refresh-ahead, and rely on load-balancer slow start for keys that were not pre-seeded. Memory headroom belongs in the same budget: ElastiCache's `reserved-memory-percent` sets aside part of a node's advertised memory for non-data uses — client output buffers on replicas, fragmentation loss, and the copy-on-write memory of a forked `bgsave` — so `maxmemory` is the advertised memory minus that reserve. It defaults to 25%, and the documentation states plainly that it should not be reduced.

Promoting a read replica that is already warm avoids building a cold node at all. Preloading keys expected to be hot — from time-of-day patterns or a launch schedule — moves the load off the moment of the spike.

## Pitfalls

- **An unthrottled warm-up loop reproduces the avalanche.** Issuing one origin load per hot key with unbounded parallelism sends the same burst the warming was meant to prevent, arriving earlier.
- **Readiness that ignores warm-up state routes traffic to a cold node.** The health check passes on process start, the load balancer adds the node, and its hit ratio is zero under full share.
- **A warm-up that aborts on the first failed key leaves the node cold.** If readiness is gated on warm-up completion, the node never enters rotation and the failure presents as reduced capacity, not as a cache error.
- **`refreshAfterWrite` without a query never fires.** Refresh is triggered by a read of an eligible entry, so an entry that stops being queried is not refreshed; it expires under `expireAfterWrite` instead.
- **`refreshAfterWrite` longer than `expireAfterWrite` is inert.** The entry is evicted before it becomes refresh-eligible, and every access is a synchronous load.
- **Warming a flat keyspace consumes origin capacity for no hit-ratio gain.** Without skew there is no leading slice whose warm covers most requests, so the pre-paid loads are spent on keys that may never be read.
- **Shadow cutover on wall-clock schedule rather than hit ratio promotes a half-filled cache.** The new cluster reports healthy while its miss rate still projects the full multiplier onto the origin.
- **A cache filled to capacity during warm-up evicts warm entries as live traffic arrives.** Seeding more keys than the eviction policy will retain converts pre-paid loads into evictions, and origin load returns.
