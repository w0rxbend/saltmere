---
title: "Cache Stampede: Coalescing, XFetch, and Stale-While-Revalidate"
date: 2026-08-10
track: microservices
summary: A hot cache key expires and thousands of concurrent misses hammer your database in the same millisecond. Here are three implementable defenses — single-flight, probabilistic early recomputation, and locking with stale serving — and when to reach for each.
reading_time: 6
tags:
  - caching
  - scaling
  - resilience
  - go
  - redis
sources:
  - title: "Optimal Probabilistic Cache Stampede Prevention (Vattani, Chierichetti, Lowenstein, VLDB 2015)"
    url: "http://www.vldb.org/pvldb/vol8/p886-vattani.pdf"
  - title: "singleflight package — golang.org/x/sync/singleflight"
    url: "https://pkg.go.dev/golang.org/x/sync/singleflight"
  - title: "Sometimes I cache: implementing lock-free probabilistic caching (Cloudflare Blog)"
    url: "https://blog.cloudflare.com/sometimes-i-cache/"
  - title: "RFC 5861 — HTTP Cache-Control Extensions for Stale Content"
    url: "https://datatracker.ietf.org/doc/html/rfc5861"
  - title: "Cache stampede (Wikipedia)"
    url: "https://en.wikipedia.org/wiki/Cache_stampede"
---

You cache the result of an expensive query — a product page, a leaderboard, a permissions blob — with a TTL of 60 seconds. It absorbs 10,000 requests per second beautifully. Then the key expires.

In the instant after expiry, all 10,000 of those requests find a cache miss simultaneously. All of them fall through to the database. All of them recompute the same value. Your backend, sized for the trickle of cache misses it normally sees, is suddenly asked to serve the full uncached load at once. Latency spikes, connections saturate, and if the recomputation is slow enough that the key stays empty while the herd piles up, the pileup grows until something falls over. This is a **cache stampede**, also called a **dog-pile** or a **thundering herd** on the cache.

The nasty part is that the failure is self-inflicted and correlated with success. The hotter the key, the bigger the stampede. Caching made you fast right up until it made you fragile. Below are three defenses that actually work, with code, and the trade-offs between them.

## Why expiry, not load, is the trigger

It helps to be precise about the mechanism. A stampede is not caused by high traffic per se — steady traffic against a warm cache is fine. It is caused by **many concurrent misses on a single key within the window it takes to recompute that key**. Two variables matter:

- **Concurrency** on the hot key (how many requests arrive during the recompute window).
- **Recompute cost** (how long the backing store takes to produce a fresh value).

Multiply them and you get the size of the herd. Every defense below attacks one of those two factors: either collapse the concurrent misses into a single backend call, or move the recomputation off the exact moment of expiry so the misses never coincide.

## Defense 1: Request coalescing (single-flight)

The simplest idea: if N callers all miss the same key at the same time, only **one** of them should actually do the work. The rest should block and share that one result. This is request coalescing, and Go ships a canonical implementation in `golang.org/x/sync/singleflight`.

The core method is:

```go
func (g *Group) Do(key string, fn func() (any, error)) (v any, err error, shared bool)
```

`Do` guarantees that for a given `key`, only one execution of `fn` is in flight at a time. Concurrent callers with the same key block until the first finishes, then all receive the same `v` and `err`. The `shared` boolean tells you whether the result was handed to more than one caller — useful as a stampede metric.

```go
var group singleflight.Group

func GetProduct(ctx context.Context, id string) (*Product, error) {
    // Fast path: serve from cache.
    if p, ok := cache.Get(id); ok {
        return p, nil
    }

    // Slow path: coalesce concurrent misses into one backend call.
    v, err, shared := group.Do(id, func() (any, error) {
        p, err := db.LoadProduct(ctx, id) // the expensive recompute
        if err != nil {
            return nil, err
        }
        cache.Set(id, p, 60*time.Second)
        return p, nil
    })
    if err != nil {
        return nil, err
    }
    if shared {
        metrics.Inc("product.coalesced")
    }
    return v.(*Product), nil
}
```

Ten thousand simultaneous misses become **one** database query; the other 9,999 goroutines park and share the answer. 

Two caveats. First, single-flight is per-process. In a fleet of 50 service instances you get at most 50 concurrent recomputes, not one — usually a fine reduction, but not one call. Pair it with a shared cache so the first instance to finish populates the value for the rest. Second, a slow or hung `fn` blocks every waiter on that key; consider `DoChan` with a `select` on `ctx.Done()` so callers can time out, and call `Forget(key)` if you don't want a failed computation to be shared. Coalescing attacks the *concurrency* factor and leaves expiry timing alone.

## Defense 2: Probabilistic early recomputation (XFetch)

Coalescing still lets the key expire and still makes some caller wait on a cold recompute. What if, instead, one lucky request refreshed the value *slightly before* it expired — while the cache is still serving the old value to everyone else? No miss ever happens, so no herd forms.

The trick is choosing *which* request refreshes early without coordination. Vattani, Chierichetti, and Lowenstein solved this optimally in their 2015 VLDB paper *Optimal Probabilistic Cache Stampede Prevention*. Alongside each cached value you store `delta` — the measured time the last recomputation took — and its absolute `expiry`. On every read, you roll the dice with this check:

```
time() - delta * beta * log(rand()) >= expiry   →   recompute now
```

Here `rand()` is uniform in (0, 1), so `log(rand())` is negative and the whole term `- delta * beta * log(rand())` is a positive "look-ahead" into the future. As the clock approaches `expiry`, the probability that any given request trips the condition rises smoothly toward certainty. Crucially, expensive keys (large `delta`) and hotter keys (more rolls per second) get refreshed *earlier and more eagerly* — exactly the keys where a stampede would hurt most. `beta` tunes the eagerness; the paper shows **`beta = 1` works well in practice** and is the recommended default. Raise it above 1 to refresh earlier, lower it toward 0 to hug the expiry.

In Go:

```go
type Entry struct {
    Value  []byte
    Delta  time.Duration // how long the last recompute took
    Expiry time.Time
}

func xfetchShouldRecompute(e Entry, beta float64) bool {
    // -log(rand) with rand in (0,1) is a positive Exp(1) sample.
    gap := float64(e.Delta) * beta * -math.Log(rand.Float64())
    return time.Now().Add(time.Duration(gap)).After(e.Expiry)
}

func GetXFetch(ctx context.Context, key string) ([]byte, error) {
    e, ok := cache.Get(key)
    if ok && !xfetchShouldRecompute(e, 1.0) {
        return e.Value, nil // overwhelmingly common path
    }

    start := time.Now()
    val, err := recompute(ctx, key)
    if err != nil {
        if ok {
            return e.Value, nil // fall back to stale on error
        }
        return nil, err
    }
    delta := time.Since(start)
    cache.Set(key, Entry{val, delta, time.Now().Add(ttl)}, ttl+slack)
    return val, nil
}
```

The beauty is that it needs no locks and no cross-node coordination — Cloudflare adopted exactly this scheme for lock-free probabilistic caching. The cost is that you occasionally recompute a value a little before you strictly had to, trading a small amount of wasted work for the near-elimination of stampedes. Note the two failure modes to handle: the recompute should serve stale on error (shown above), and the physical TTL in the store must outlive the logical `expiry` by some `slack` so early refreshes always have an old value to fall back on.

## Defense 3: Locking + stale-while-revalidate

The third approach is the most operationally familiar: on a miss, one caller takes a **lock** (e.g. Redis `SET lock:key token NX PX 5000`), recomputes, and writes the value; everyone else, rather than blocking on a cold miss, is served the **stale** previous value while the refresh happens in the background. This is precisely the semantics of HTTP's `stale-while-revalidate` from RFC 5861: keep serving the expired representation for a bounded window while a single asynchronous revalidation runs.

Concretely, store the value with a logical freshness timestamp but a longer physical TTL. When a request finds the value stale-but-present, it tries `SET NX` on a companion lock key. The winner refreshes asynchronously; the loser and all other readers immediately get the stale value. No reader ever waits on the backend, and only the lock winner touches it.

The trade-off is **staleness plus moving parts**: you need a lock with a sane expiry (so a crashed holder can't wedge the key forever), a background refresh path, and tolerance for serving data that is a few seconds old. If your data genuinely cannot be stale, this is not your tool — reach for coalescing instead.

## Choosing between them

- **Single-flight** — cheapest to adopt, no staleness, in-process only. Best first move; combine with a shared cache. Attacks concurrency.
- **XFetch** — lock-free and coordination-free, scales across nodes, prevents the miss from ever happening. Best default for hot read-heavy keys. Attacks expiry timing; costs a little redundant work.
- **Lock + stale-while-revalidate** — zero reader latency even under refresh, at the cost of bounded staleness and more infrastructure. Best when recompute is very expensive and slightly-old data is acceptable.

These are not mutually exclusive. A robust setup often runs XFetch to avoid coincident misses, wraps the recompute in single-flight per node as a backstop, and serves stale on error. Each layer removes a different way the herd can form.

**Try next:** Instrument your top ten cache keys with the `shared` counter from single-flight and a "recompute triggered" counter from an XFetch check, then run a load test that expires a hot key under 5,000 rps and watch how many backend calls each defense actually collapses.
