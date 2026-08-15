---
title: "Cache Stampede: Coalescing, XFetch, and Stale-While-Revalidate"
date: 2026-08-10
track: microservices
summary: When a hot cache key expires, every concurrent miss falls through to the backing store at once. Three implementable defenses — single-flight coalescing, probabilistic early recomputation, and locking with stale serving — with the cost each one imposes.
reading_time: 7
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

**Gist.** When a heavily read cache key expires, every request arriving during the recomputation window misses simultaneously and falls through to the backing store, which was sized for the trickle of misses a warm cache normally produces. Three defenses attack this: **single-flight coalescing** collapses concurrent misses into one backend call, **probabilistic early recomputation (XFetch)** refreshes the value before expiry so the miss never occurs, and **locking with stale-while-revalidate** serves the expired value while one holder refreshes. The costs are, respectively, a shared fate among waiters, a small amount of redundant recomputation, and bounded staleness plus lock machinery.

## The trigger is expiry, not load

A **cache stampede** — also called a **dog-pile** or a **thundering herd** on the cache — is not caused by high traffic alone. Steady traffic against a warm cache produces no backend load at all. The stampede is caused by **many concurrent misses on a single key within the window it takes to recompute that key**. Two quantities set the size of the herd:

- **Arrival rate on the hot key**: requests per second directed at that one key.
- **Recompute cost**: the time the backing store takes to produce a fresh value.

Their product bounds the number of redundant backend calls, since every request arriving during the recompute window finds the key absent. A key absorbing 10,000 requests per second with a one-second recompute admits on the order of 10,000 concurrent misses; if the recomputation is slow enough that the key stays empty while the herd accumulates, the queue at the backing store grows monotonically until a connection pool or a timeout budget is exhausted. The failure correlates with success: the hotter the key, the larger the herd.

Every defense below reduces one of the two factors — either collapse the concurrent misses into a single backend call, or displace the recomputation away from the exact instant of expiry so that misses do not coincide.

## Defense 1: request coalescing (single-flight)

The invariant is stated in one line: **for a given key, at most one execution of the recompute function is in flight at a time.** Callers that arrive while an execution is running block and receive the same result. Go's `golang.org/x/sync/singleflight` package is a canonical implementation:

```go
func (g *Group) Do(key string, fn func() (any, error)) (v any, err error, shared bool)
```

`Do` runs `fn` for the first caller of a given `key` and parks every concurrent caller of the same key until it returns; all of them then receive the same `v` and `err`. The `shared` boolean reports whether the result was delivered to more than one caller, which makes it a usable stampede metric.

```go
var group singleflight.Group

func GetProduct(ctx context.Context, id string) (*Product, error) {
    if p, ok := cache.Get(id); ok {
        return p, nil
    }

    // Slow path: concurrent misses on this id share one backend call.
    v, err, shared := group.Do(id, func() (any, error) {
        p, err := db.LoadProduct(ctx, id)
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

Two limits follow from the mechanism. First, **the group is per-process**: a fleet of 50 instances admits up to 50 concurrent recomputes, not one. A shared cache narrows this, because the first instance to complete publishes the value for the others. Second, **every waiter on a key shares the fate of the single in-flight call**: a hung `fn` blocks them all, and a failure is returned to all of them. `DoChan` returns a channel, allowing a `select` on `ctx.Done()` so that waiters can abandon the call, and `Forget(key)` drops the in-flight entry so a subsequent caller starts a fresh execution rather than joining a doomed one.

Coalescing reduces the concurrency factor and leaves expiry timing untouched: one caller still pays the full cold-recompute latency.

## Defense 2: probabilistic early recomputation (XFetch)

XFetch removes the miss instead of deduplicating it. One request refreshes the value *before* expiry while the cache continues serving the existing value to everyone else. The difficulty is selecting which request refreshes early **without coordination between readers**.

Vattani, Chierichetti and Lowenstein give an optimal solution in *Optimal Probabilistic Cache Stampede Prevention* (VLDB 2015). Alongside each cached value the implementation stores `delta`, the measured duration of the last recomputation, and the absolute `expiry`. Each read evaluates:

```
time() - delta * beta * log(rand()) >= expiry   →   recompute now
```

`rand()` is uniform on (0, 1), so `log(rand())` is negative and `- delta * beta * log(rand())` is a positive exponentially distributed look-ahead. Three consequences are load-bearing. As the clock approaches `expiry` the probability that any single read trips the condition rises smoothly toward certainty, so the refresh is spread over an interval rather than concentrated at one instant. **Expensive keys refresh earlier**, because the look-ahead scales with `delta`. **Hot keys refresh earlier**, because more reads per second means more draws from the distribution. Both are the keys where a stampede does the most damage. `beta` tunes eagerness; the paper reports that **`beta = 1` works well in practice**. Values above 1 refresh earlier, values toward 0 hug the expiry.

The scheme requires no lock and no cross-node coordination — Cloudflare describes implementing it as lock-free probabilistic caching. Two structural requirements attach to it. The recompute path should **fall back to the still-present old value on error**, since the entry has not expired yet. And **the physical time-to-live (TTL) in the store must exceed the logical `expiry` by some slack**, so that an early refresh always has an old value available to serve and to fall back on.

### Implementation sketch (Scala)

```scala
final case class Entry[A](value: A, delta: FiniteDuration, expiry: Instant)

// Exp(1) look-ahead: -log(U) for U uniform on (0,1).
def shouldRecompute[A](e: Entry[A], beta: Double, now: Instant): Boolean =
  val gap = e.delta.toNanos * beta * -math.log(Random.nextDouble())
  now.plusNanos(gap.toLong).isAfter(e.expiry)

def get[A](key: String, ttl: FiniteDuration, slack: FiniteDuration)(
    recompute: String => A
): A =
  val now = Instant.now()
  cache.get(key) match
    case Some(e) if !shouldRecompute(e, beta = 1.0, now) => e.value
    case existing =>
      val start = System.nanoTime()
      try
        val fresh = recompute(key)
        val delta = (System.nanoTime() - start).nanos
        // Physical TTL outlives the logical expiry so an early
        // refresh always finds a previous value to serve.
        cache.put(key, Entry(fresh, delta, now.plusNanos(ttl.toNanos)), ttl + slack)
        fresh
      catch
        case NonFatal(err) => existing.map(_.value).getOrElse(throw err)
```

`Random.nextDouble()` returns a value in [0, 1). A draw of exactly 0 makes `-math.log(0)` infinite, and `gap.toLong` then saturates at `Long.MaxValue`, which `Instant.plusNanos` rejects with an arithmetic overflow — production code clamps the gap rather than relying on the draw never being 0.

## Defense 3: locking with stale-while-revalidate

The third approach separates the reader path from the refresh path. On finding a stale-but-present value, a caller attempts to take a lock — in Redis, `SET lock:key token NX PX 5000`, which sets the key only if absent and attaches a 5-second expiry. The winner refreshes in the background; the loser and every other reader are served the **stale** value immediately. This is the semantics RFC 5861 defines for HTTP's `stale-while-revalidate`: continue serving the expired representation for a bounded window while an asynchronous revalidation runs; the RFC bounds the staleness window but does not itself specify how the revalidating caller is chosen.

The storage layout is the same one XFetch needs — a logical freshness timestamp with a longer physical TTL — but the selection of the refreshing caller is by mutual exclusion rather than by random draw. **No reader waits on the backing store**, and only the lock winner touches it.

The costs are bounded staleness and additional moving parts: a lock whose expiry is short enough that a crashed holder cannot wedge the key indefinitely, a background refresh path with its own failure handling, and a tolerance for data some seconds old. Where the data cannot be stale, this defense does not apply.

## Choosing between them

- **Single-flight** — no staleness, no additional infrastructure, per-process scope. Reduces concurrency; one caller still pays cold-recompute latency.
- **XFetch** — lock-free and coordination-free, so it holds across nodes. Prevents the miss from occurring, at the cost of some recomputation performed earlier than strictly necessary.
- **Lock plus stale-while-revalidate** — no reader ever blocks on the backing store, at the cost of bounded staleness and lock lifecycle management.

The three compose. XFetch avoids coincident misses, per-node single-flight bounds the damage when one occurs anyway, and serving stale on error covers a failed recomputation. Each layer removes a different route by which the herd forms.

## Pitfalls

- **Physical TTL equal to logical expiry under XFetch.** The early refresh finds no entry, so the read degrades to an ordinary cold miss and the stampede returns; the entry must outlive its logical expiry by a slack interval.
- **Recompute errors evicting the entry.** A transient backend failure that clears the key converts one failed refresh into a full stampede on the next read. Both XFetch and stale-while-revalidate depend on the old value remaining present.
- **Single-flight treated as a global guarantee.** The group is per-process, so an N-instance fleet still issues up to N concurrent recomputes on the same key.
- **No timeout on the coalesced call.** Every waiter blocks for as long as the single in-flight `fn` runs; without `DoChan` and a context deadline, one hung recompute stalls all callers of that key.
- **Shared failure results.** A returned error is delivered to every waiter, so a single transient failure is amplified across the whole herd unless `Forget` drops the entry.
- **Lock TTL shorter than the recompute.** The lock expires mid-refresh, a second caller acquires it and starts a duplicate recompute, and the original holder may overwrite the newer value on completion.
- **Uniform TTLs across many keys.** Keys populated together expire together, producing a stampede across a whole key set rather than one key; per-key defenses do not address correlated expiry.
