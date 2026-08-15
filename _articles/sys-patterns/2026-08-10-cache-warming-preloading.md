---
title: "Cache Warming and Preloading: Surviving the Cold Start Before Traffic Finds You"
date: 2026-08-10
track: sys-patterns
summary: "A fresh, flushed, or failed-over cache serves everything as a miss, and that flood of misses can knock over the very origin the cache was meant to protect. This article covers the cold-cache avalanche, then five warming strategies — deploy/startup warm-up, refresh-ahead, staged traffic ramp, shadow/dual-cache warming, and working-set capture — with concrete code for seeding top-N keys and a Caffeine refresh-ahead loader. Ties warming to failover and autoscaling, where every new node starts cold."
reading_time: 6
tags: [caching, cache-warming, preloading, cold-start, refresh-ahead, failover, autoscaling, interview-prep]
sources:
  - title: "Caffeine Wiki — Refresh (refreshAfterWrite, CacheLoader.reload)"
    url: "https://github.com/ben-manes/caffeine/wiki/Refresh"
  - title: "Netflix TechBlog — Cache Warming: Agility for a Stateful Service (EVCache)"
    url: "https://netflixtechblog.com/cache-warming-agility-for-a-stateful-service-2d3b1da82642"
  - title: "Amazon ElastiCache — Caching Strategies (Lazy Loading vs Write-Through)"
    url: "https://docs.aws.amazon.com/AmazonElastiCache/latest/red-ug/Strategies.html"
  - title: "Amazon ElastiCache — Managing Reserved Memory for Valkey and Redis OSS"
    url: "https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/redis-memory-management.html"
  - title: "Aerospike — Cache Warming Explained: Benefits, Pitfalls, and Alternatives"
    url: "https://aerospike.com/blog/cache-warming-explained/"
---

A cache earns its keep by absorbing reads the origin would otherwise serve. But a cache is only useful once it's *full of the right things*. In the moment right after it comes up — a fresh deploy, a `FLUSHALL`, a Multi-AZ failover, a new autoscaled node — the cache is empty, and empty means every request is a **miss**. Each miss falls through to the origin. If your steady-state hit ratio is 95%, going cold means the database instantaneously sees roughly **20× its normal read load**. That surge is a self-inflicted [cache avalanche](/articles/distributed-systems/2026-08-10-cache-penetration-breakdown-avalanche): the cache layer, the thing meant to shield the database, becomes the reason the database falls over.

This is the **cold-cache problem**, and it's dangerous because the failure correlates with your worst moments: caches go cold precisely when something already went wrong — a node died, a region flipped, you scaled out under a spike. AWS's lazy-loading guidance describes it plainly: "When a node fails and is replaced by a new, empty node, your application continues to function, though with increased latency. As requests are made to the new node, each cache miss results in a query of the database." That "increased latency" is the optimistic framing. Under load, the honest framing is: the new node points a firehose at your origin.

## Warm-up vs. lazy fill: when is it worth it?

The default is **lazy loading** (a.k.a. cache-aside): populate on miss, organically. It's simple, and it only ever caches data someone actually asked for. For most services, most of the time, lazy fill is correct — you don't need to warm a cache that comes up during a quiet window and fills gently.

Warming earns its complexity when **the cold window overlaps with real load** and the origin can't absorb the miss flood. Concretely, warm when:

- The origin is expensive or fragile relative to peak QPS (a single primary DB, a slow downstream, an ML model).
- The working set is **skewed** — a small hot set serves most traffic — so a little warming buys most of the benefit.
- Cold events are *scheduled or predictable*: deploys, blue/green cutovers, autoscaling, planned failover drills.
- Recompute cost per entry is high (rendered pages, aggregations, embeddings).

Skip warming when the keyspace is huge and flat (no hot set to prioritize), when misses are cheap, or when the cache fills faster than traffic ramps anyway.

## Strategy 1: Proactive warm-up on deploy/startup

The most direct fix: before a node accepts traffic, push the **top-N hot keys** into it. You get the hot set from somewhere you already have — yesterday's access logs, an analytics rollup, a `hotkeys` sample, or a dump of the currently-hot keys from a sibling node. Then, crucially, gate readiness on the warm-up so the load balancer doesn't route to a cold instance.

```java
// Warm the top-N hot keys before this node reports "ready".
// hotKeys comes from log/analytics rollup, ordered by request count desc.
public void warmUp(List<String> hotKeys, int n) {
    var top = hotKeys.stream().limit(n).toList();
    var pool = Executors.newFixedThreadPool(16); // bounded: don't DDoS your own DB
    var rate = RateLimiter.create(2_000);          // cap origin QPS during warm-up
    var inflight = new ArrayList<Future<?>>();

    for (String key : top) {
        inflight.add(pool.submit(() -> {
            if (cache.get(key) != null) return;   // already present, skip
            rate.acquire();                         // throttle the origin
            Value v = origin.load(key);             // the expensive read we're pre-paying
            if (v != null) cache.put(key, v);
        }));
    }
    for (var f : inflight) { try { f.get(); } catch (Exception e) { /* log, continue */ } }
    pool.shutdown();
    readiness.markWarm();                            // only now: accept traffic
}
```

Two details separate a working warm-up from an outage. First, **throttle the warm-up itself** — a warm-up loop that fires N thousand parallel loads *is* the avalanche you're trying to prevent; bound the pool and rate-limit the origin. Second, **prioritize by value, not just recency**: sort candidate keys by (request frequency × recompute cost), warm the top slice, and stop when marginal hit-ratio gain flattens. Warming the top 1–5% of keys typically recovers the bulk of your hit ratio.

## Strategy 2: Refresh-ahead so hot entries never expire under load

Warming fixes the *initial* cold cache. Refresh-ahead fixes a subtler cold spot: a hot key whose TTL expires at peak, forcing a synchronous miss (and, if many keys share an expiry, a [stampede](/articles/microservices/2026-08-10-cache-stampede-request-coalescing)). The idea is to **refresh popular entries in the background before they go stale**, so readers always hit a warm value.

Caffeine implements this directly with `refreshAfterWrite`. Per the Caffeine docs, it "will make a key eligible for refresh after the specified duration, but a refresh will only be actually initiated when the entry is queried" — and the refresh is **asynchronous**: the old cached value keeps being served to callers while the new value loads in the background. That's the whole trick — no reader ever blocks on a reload.

```java
LoadingCache<Key, Graph> graphs = Caffeine.newBuilder()
    .maximumSize(10_000)
    .refreshAfterWrite(Duration.ofMinutes(1))   // eligible to refresh after 1 min...
    .expireAfterWrite(Duration.ofMinutes(5))    // ...but only truly expires at 5 min
    .build(key -> loadExpensiveGraph(key));      // CacheLoader for load AND async refresh
```

Combining `refreshAfterWrite` (short) with `expireAfterWrite` (longer) gives the best of both: hot keys, which get queried, are refreshed silently and never expire under load; cold keys, which aren't queried, eventually hard-expire and free memory. Override `CacheLoader.reload(key, oldValue)` if you want refresh to reuse the previous value (e.g. batch reloads, or serve-stale-on-error — Caffeine keeps the old value if the reload throws). This is the pattern the [caching strategies overview](/articles/sys-patterns/2026-08-10-tinylfu-cache-admission-control/) recommends whenever recompute is expensive and staleness of a few seconds is acceptable.

## Strategy 3: Staged traffic ramp after failover

When a fresh node comes up cold, don't hand it 100% of its share instantly. **Ramp traffic gradually** — send it 5%, then 20%, then 50%, over tens of seconds — so it fills its working set from real reads while the origin sees only a fraction of the miss load at any instant. This is the load-shedding dual of warm-up: instead of pre-filling before traffic, you meter the traffic to match the fill rate. Load balancers with "slow start" (e.g. weighted ramp on newly-healthy targets) implement exactly this, and it composes well with autoscaling, where scale-out events add cold nodes mid-spike.

## Strategy 4: Shadow / dual-cache warming before cutover

For blue/green cache migrations or version upgrades, run the **new cache in shadow**: mirror production reads into it (and writes, if applicable) so it fills with the live working set *before* you cut over. Only flip traffic once its hit ratio matches the incumbent's. This avoids the classic migration outage where you promote a brand-new, empty cluster into the request path at full load. It costs you two caches running in parallel for a window — the price of a safe cutover.

## Strategy 5: Capturing the working set (mirror/replay)

The most robust warm is to copy the *actual* working set rather than guess it. Netflix's EVCache does this at petabyte scale: instead of replaying traffic, they enumerate keys on source nodes (via the memcached LRU-crawler), dump values to S3 in chunks, and repopulate the target through an SQS-decoupled pipeline. Their numbers are a useful reality check on how big "warm" can get — a replaced instance "warmed up in less than 15 minutes with about 2.2 GB," a 12 TB / 500-million-item replica in ~2 hours, and their largest run warmed 700 TB across 380 nodes. The general technique — **mirror production reads into the new cache**, or dump-and-restore the hot set — gives you a warm cache without touching the origin at all, which is exactly what you want when the origin is the fragile part.

## Failover and autoscaling: every new node starts cold

Tie it together: the two most common sources of cold caches in production are **failover** and **autoscaling**, and both are automatic, so the warming must be automatic too. A Multi-AZ failover promotes a replica — warm if it was replicating, cold if it's a freshly built node. An autoscaling scale-out adds nodes *because* load is high, meaning cold nodes join at the worst possible moment. Bake warming into the lifecycle: gate the readiness/health check on warm-up completion (Strategy 1), keep hot entries fresh with refresh-ahead (Strategy 2), and use load-balancer slow-start (Strategy 3) as the safety net for the keys you didn't pre-seed. And leave headroom: AWS ElastiCache's `reserved-memory-percent` exists partly so a node can absorb this kind of burst activity without hitting swap during the exact window it's under stress.

Predictive/read-replica warming is the advanced move: promote a **read replica that's already warm** instead of building a cold node, or pre-load keys you expect to be hot (time-of-day, launch schedules) before traffic arrives. The cheapest miss is the one that hits a value you loaded a minute early.

**Try next:** measure your real cold-cache blast radius — take your steady-state hit ratio, invert it to get the origin QPS multiplier during a cold start, and check whether your database can survive that number. If it can't, wire warm-up into your readiness probe and add `refreshAfterWrite` to your hottest loader.
