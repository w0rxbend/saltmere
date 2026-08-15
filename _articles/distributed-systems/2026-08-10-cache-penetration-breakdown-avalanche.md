---
title: "Penetration, Breakdown, Avalanche: The Three Cache Failure Modes"
date: 2026-08-10
track: distributed-systems
summary: "Three cache failure modes named in Chinese engineering literature — penetration (queries for keys that exist nowhere), breakdown (one hot key expires under load), and avalanche (many keys expire at once, or the cache tier dies) — precisely defined, each with its own defense: a bloom-filter gate with negative caching, a single-flight rebuild, and time-to-live jitter."
reading_time: 8
tags:
  - caching
  - redis
  - system-design
  - bloom-filter
  - reliability
sources:
  - title: "Redis Cache Problems: Penetration, Breakdown and Avalanche — Charlie Feng's Tech Space"
    url: "https://shayne007.github.io/2025/06/10/Redis-Cache-Problems-Penetration-Breakdown-and-Avalanche/"
  - title: "Detailed explanation of Redis caching problems — Alibaba Cloud"
    url: "https://www.alibabacloud.com/en/knowledge/developer1/detailed-explanation-caching-problems"
  - title: "A Crash Course in Caching (Final Part) — Alex Xu, ByteByteGo"
    url: "https://blog.bytebytego.com/p/a-crash-course-in-caching-final-part"
  - title: "Cache stampede — Wikipedia"
    url: "https://en.wikipedia.org/wiki/Cache_stampede"
  - title: "How to Use Bloom Filters for Cache Penetration Prevention in Redis — OneUptime"
    url: "https://oneuptime.com/blog/post/2026-03-31-redis-how-to-use-bloom-filters-for-cache-penetration-prevention-in/view"
---

**Gist.** A cache in front of a database holds read load away from it; three distinct events return that load to the database in full. Penetration (缓存穿透) is traffic for keys with no backing row, so the miss never becomes a hit; breakdown (缓存击穿) is concurrent rebuild of one expired hot key; avalanche (缓存雪崩) is the simultaneous expiry of many keys, or loss of the cache tier itself. Each defense — negative caching plus a bloom-filter gate, a single-flight rebuild lock, time-to-live (TTL) jitter — buys protection by accepting staleness, added latency on the miss path, or memory spent on keys that hold no data.

## Three failures that share an outcome and not a cause

All three end with a load spike at the database, which is why they are conflated. The distinguishing property is the *state of the key set* at the moment of the spike:

- **Penetration**: requests name keys absent from both cache and database, so the miss path terminates without writing anything back to the cache.
- **Breakdown**: a single very hot key expires, and every request that was being served from it misses in the same interval and rebuilds independently.
- **Avalanche**: a large set of keys expires within the same short window, or the cache node becomes unreachable, so a broad fraction of read traffic falls through together.

The compressed form is "no data", "one key", "many keys".

## Penetration: queries for data that is not there

An ordinary miss is self-healing: the reader misses, reads the database, writes the value back, and the next request hits. **Penetration breaks the loop because the database returns no row, and an absent row is not normally written to the cache.** A request for `user:999999999` misses, queries the database, gets nothing, writes nothing; the next identical request repeats the full path. The cache absorbs none of this traffic, so a caller enumerating non-existent identifiers reaches the database at request rate.

Two defenses are standard, and they compose.

**Cache the negative result.** When the database returns no row, write a sentinel value under that key with a **TTL far shorter than the one used for real data**. Repeated queries for the missing key now terminate at the cache. The short TTL bounds the interval during which a key that later gains a real value keeps answering "missing". The cost is memory proportional to the number of distinct missing keys observed within one negative TTL, which is why the sentinel TTL must stay short under adversarial enumeration.

**Gate on a bloom filter.** A bloom filter is a probabilistic set membership structure answering either "definitely not present" or "possibly present". Pre-loaded with every valid key, it is consulted before cache and database. **The load-bearing property is the absence of false negatives: a key the filter rejects provably has no entry in the filter's key set, so rejecting it cannot drop a valid request.** A false positive costs one wasted pass through the normal miss path, which already handles a missing row. The filter's own concerns — sizing, hash count, and admitting newly created keys, since a standard bloom filter supports no deletion — are treated in the dedicated bloom-filter article in this series.

## Breakdown: one hot key, many concurrent rebuilders

Breakdown is a stampede narrowed to one key. A single entry serving a high request rate expires at some instant; in the interval between expiry and the first successful write-back, **every in-flight request misses and each independently issues the same expensive query**. Database load for that key goes from zero to the full concurrent request count. This is the [cache stampede](https://en.wikipedia.org/wiki/Cache_stampede) or thundering herd, concentrated on the hottest key.

**Mutex / single-flight rebuild.** On a miss, readers contend for a lock; Redis `SET key value NX EX ttl` serves as a cheap distributed mutex, since `NX` (set-if-not-exists) makes acquisition atomic. **Exactly one reader wins and rebuilds; the losers wait and re-read rather than querying the database.** The invariant is that at most one rebuild per key is in flight, so database load for that key is bounded by one query per rebuild interval instead of one per request. Two costs follow: the losers pay the wait latency, and the lock TTL is a liveness fallback — if the winner dies before releasing, the key stays unbuilt until the lock expires.

**Logical expiry with asynchronous refresh.** The expiry timestamp is stored *inside* the cached value and the Redis entry is given no TTL, or a very long one. Readers therefore always find a value and return immediately, possibly stale. A reader that observes a passed logical timestamp triggers a background refresh, guarded by the same mutex, while all readers continue serving the stale value. **No request ever blocks on a rebuild, so the herd does not form; the trade is that readers observe stale data for the duration of the refresh, and the key is never evicted by expiry.**

Probabilistic early expiration (XFetch) and request coalescing via singleflight are treated at [/articles/microservices/2026-08-10-cache-stampede-request-coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing). Those are the general stampede tools; the mutex and logical-expiry patterns are the ones named under "breakdown".

## Avalanche: many keys expire together

Avalanche is breakdown scaled across the key space, with two common triggers. The first is **synchronized TTLs**: a warming pass writes a large batch of entries with an identical TTL, and one TTL later they expire in the same second, moving the database from near-idle to full read traffic in one step. The second is **loss of the cache node**: if the cache is unreachable, the entire read stream falls through at once, and a cache outage becomes a database outage.

**TTL jitter.** A constant TTL applied to a batch preserves the batch's arrival distribution into its expiry distribution. Adding a random spread scatters expiries across a window whose width is the spread, so the peak miss rate falls as the window widens. This is the cheapest of the three defenses and requires no coordination.

**Multi-level cache.** A small in-process first level (L1) in front of the shared second level (L2) continues answering while L2 entries expire or L2 is briefly unreachable, flattening the spike that reaches the database. The cost is a second copy with its own staleness window and no cross-process invalidation.

**Circuit breaker and concurrency limit on the miss path.** Treating the database as a protected resource, a breaker trips when miss traffic crosses a threshold and sheds or queues the excess, so a fraction of requests fail fast instead of the database failing all of them. A concurrency limiter on the rebuild path enforces the same bound continuously.

**High availability for the cache tier.** Since node loss is itself an avalanche trigger, a replicated or clustered deployment prevents a single node failure from removing the cache tier.

## Summary table

| Failure | Trigger | Affected keys | Primary defense | Secondary defense |
|---|---|---|---|---|
| **Penetration** | Key has no backing row | Non-existent keys | Cache the negative (short TTL) | Bloom-filter gate |
| **Breakdown** | One hot key expires | A single very hot key | Mutex / single-flight rebuild | Logical expiry + async refresh |
| **Avalanche** | Many keys expire together, or cache node dies | Broad fraction of the key space | TTL jitter | Multi-level cache, circuit breaker, high availability |

### Implementation sketch (Scala)

The three defenses on one read path. `redis` stands for any client exposing `get`, `setEx`, `setNxEx` and `del`; the point is the ordering of the checks, not the client API.

```scala
val NegativeSentinel = " "
val NegativeTtl = 60.seconds
val DataTtl = 3600.seconds

def jittered(base: FiniteDuration, spread: Double = 0.2): FiniteDuration =
  // Spreads a warmed batch over [base, base * (1 + spread)] instead of one instant.
  (base.toSeconds * (1 + Random.nextDouble() * spread)).toLong.seconds

def read(id: Long): Option[Row] =
  if !bloom.mightContain(id) then None          // no false negatives: safe to reject
  else
    val key = s"user:$id"
    redis.get(key) match
      case Some(NegativeSentinel) => None
      case Some(raw)              => Some(decode(raw))
      case None                   => rebuild(key, id)

private def rebuild(key: String, id: Long): Option[Row] =
  val lock = s"lock:$key"
  if redis.setNxEx(lock, "1", 10.seconds) then
    try
      db.load(id) match
        case Some(row) => redis.setEx(key, encode(row), jittered(DataTtl)); Some(row)
        case None      => redis.setEx(key, NegativeSentinel, NegativeTtl); None
    finally redis.del(lock)
  else
    // Lost the race: the winner is rebuilding, so re-read instead of querying the database.
    Thread.sleep(50)
    read(id)
```

## Pitfalls

- **A negative sentinel indistinguishable from a real serialized value** makes a legitimate row read as missing; choose a sentinel that cannot occur in the encoding, and branch on it before decoding.
- **A long negative TTL under enumeration** turns the penetration defense into a memory-exhaustion path, since every distinct probed key occupies an entry until it expires.
- **A standard bloom filter supports no deletion**, so keys removed from the backing store keep passing the gate and fall through to the normal miss path; only the false-positive rate degrades, but it degrades monotonically until the filter is rebuilt.
- **A rebuild lock TTL shorter than the rebuild** lets a second reader acquire the lock while the first is still working, restoring the duplicate query the lock was meant to remove; a TTL longer than the rebuild leaves the key unserved for the remainder of the TTL if the winner dies.
- **Recursive re-read after losing the lock, with no bound**, spins if the winner fails permanently; each retry pays the sleep and finds the key still absent.
- **Logical expiry means the entry is never evicted by TTL**, so keys that stop being read consume cache memory indefinitely, and a refresh that fails silently serves stale data with no expiry to end it.
- **Jitter applied only to new writes** leaves the already-warmed synchronized batch clustered; the first synchronized expiry still occurs, and only subsequent generations are spread.
- **A circuit breaker on the database with no fallback** converts an avalanche into an outage for the shed fraction; the breaker bounds database load, it does not make the reads succeed.
