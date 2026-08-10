---
title: "Penetration, Breakdown, Avalanche: The Three Cache Failure Modes"
date: 2026-08-10
track: distributed-systems
summary: The three classic cache failure modes from Chinese engineering literature that show up in every system-design interview — penetration (queries for keys that don't exist), breakdown (one hot key expires under load), and avalanche (many keys expire at once) — precisely defined and each with its own defense, plus code for a bloom-filter guard, a mutex rebuild, and TTL jitter.
reading_time: 7
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

## Three failures that look alike and aren't

A cache sits in front of a database to absorb read traffic. When it works, the database sees a trickle. The interesting question — and the one interviewers reach for — is what happens the moment the cache *stops* absorbing that traffic. There are three distinct ways this happens, and Chinese engineering literature named them so precisely that the terms have become standard vocabulary: 缓存穿透 (cache **penetration**), 缓存击穿 (cache **breakdown**, sometimes translated "hotspot invalidation"), and 缓存雪崩 (cache **avalanche**).

They are constantly confused because all three end the same way — a spike of load slams the database. But the *cause* differs in each, and so does the defense. Getting the distinction crisp is most of the battle. Here is the one-sentence version of each:

- **Penetration**: requests ask for keys that *don't exist anywhere*, so they miss the cache *and* miss the database, every time.
- **Breakdown**: a *single very hot key* expires, and the flood of concurrent requests that were all being served from it now miss simultaneously and rebuild it at once.
- **Avalanche**: a *large number of keys* expire at the same instant (or the cache node dies), so a broad swath of traffic falls through together.

Penetration is about keys with no backing data. Breakdown is a stampede on *one* key. Avalanche is *many* keys failing at once. Keep those three nouns — "no data," "one key," "many keys" — and you won't mix them up.

## Penetration: queries for data that isn't there

A normal cache miss is self-healing: you miss, you read the database, you populate the cache, and the next request hits. Penetration breaks that loop because there is nothing to populate the cache *with*. Someone requests `user:999999999`, which doesn't exist. The cache misses. The database returns empty. Nothing gets cached, because "nothing" isn't a value we normally store. The next identical request repeats the whole trip. An attacker who enumerates non-existent IDs can drive every request straight to the database — the cache might as well not be there.

There are two standard defenses, and good systems use both.

**Cache the negative result.** When the database returns empty, store a sentinel (an empty marker) under that key with a *short* TTL — say 30 to 60 seconds, far shorter than the TTL for real data. Now repeated queries for the missing key hit the cache and stop. The short TTL bounds the damage if the key later gains a real value.

**Reject non-existent keys up front with a bloom filter.** A bloom filter is a probabilistic set that answers "definitely not present" or "possibly present." Pre-load it with every valid key. Check it before touching cache or database; if it says "definitely not present," return empty immediately. False positives (a non-existent key slipping through) just fall back to the normal path, so they cost nothing but a wasted lookup. False negatives are impossible, which is the property that makes it safe as a gate.

```python
# Penetration guard: bloom filter gate + negative caching
NEGATIVE = "\x00"           # sentinel for "known missing"
NEG_TTL  = 60               # short TTL for negatives
DATA_TTL = 3600

def get_user(uid):
    # 1. Reject keys that provably don't exist. No false negatives.
    if uid not in bloom:                 # bloom.__contains__
        return None

    key = f"user:{uid}"
    cached = redis.get(key)
    if cached is not None:
        return None if cached == NEGATIVE else deserialize(cached)

    row = db.query_user(uid)
    if row is None:
        # 2. Cache the miss so repeats don't reach the DB.
        redis.set(key, NEGATIVE, ex=NEG_TTL)
        return None

    redis.set(key, serialize(row), ex=DATA_TTL)
    return row
```

The bloom filter has its own tradeoffs — sizing, hash count, and how you handle newly added keys — covered in its own article in this series. Here it is just the front gate.

## Breakdown: one hot key, a thousand rebuilders

Breakdown is a stampede narrowed to a single key. Picture a product page for a flash sale being served ten thousand times a second entirely from one cache entry. That entry has a TTL, and at some instant it expires. In the microseconds before anyone repopulates it, all ten thousand in-flight requests miss, and *each one* decides to rebuild the value from the database. The database, which was seeing zero load for this key, suddenly takes ten thousand identical expensive queries. This is the classic thundering-herd / [cache stampede](https://en.wikipedia.org/wiki/Cache_stampede), concentrated on the hottest key you have.

Two defenses dominate.

**Mutex / single-flight rebuild.** On a miss, make requests contend for a lock (a Redis `SET key val NX EX ttl` works as a cheap distributed mutex). Exactly one request wins, rebuilds the value, and writes it back. The losers wait briefly and re-read the now-populated cache instead of hitting the database.

```python
def get_hot(key):
    val = redis.get(key)
    if val is not None:
        return deserialize(val)

    lock = f"lock:{key}"
    # Only one caller acquires the lock (NX = set-if-not-exists).
    if redis.set(lock, "1", nx=True, ex=10):
        try:
            val = db.load(key)
            redis.set(key, serialize(val), ex=jittered(3600))
            return val
        finally:
            redis.delete(lock)
    else:
        # Someone else is rebuilding — back off and read their result.
        time.sleep(0.05)
        return get_hot(key)
```

**Logical (never-)expiry with async refresh.** Instead of letting Redis expire the key, store an expiry timestamp *inside* the value and give the Redis entry no TTL (or a very long one). Readers always get a value immediately — even a slightly stale one — and when a reader notices the logical timestamp has passed, it kicks off a *background* refresh (guarded by the same mutex) while everyone keeps serving the stale value. No request ever blocks on a rebuild, so the herd never forms.

A related, more statistical approach — probabilistic early expiration (XFetch) and request coalescing via singleflight — is covered in depth at [/articles/microservices/2026-08-10-cache-stampede-request-coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing). Those are the general-purpose stampede tools; the mutex and logical-expiry patterns above are the ones interviewers expect under the "breakdown" name.

## Avalanche: everything expires at once

Avalanche is breakdown scaled out across keys. It has two common triggers. The first is *synchronized TTLs*: you warm ten million cache entries at deploy time, all with `ex=3600`, and exactly one hour later they all expire in the same second. The database goes from near-idle to full read traffic instantly. The second trigger is *the cache node dying* — if Redis falls over, 100% of reads fall through to the database at once, which is often enough to take the database down too, turning a cache outage into a full outage.

The defenses attack both triggers:

**TTL jitter.** Never use a constant TTL for a batch of keys. Add a random spread so expirations scatter across a window instead of clustering on one instant.

```python
import random
def jittered(base, spread=0.2):
    # base=3600, spread=0.2 -> TTL uniformly in [3600, 4320]
    return int(base * (1 + random.random() * spread))
```

This one line is the single highest-leverage avalanche defense and costs nothing.

**Multi-level cache.** Put a small in-process L1 (local memory) in front of the shared L2 (Redis). Even if a wave of L2 entries expires, or Redis itself is briefly unreachable, the L1 absorbs a large fraction of reads and flattens the spike reaching the database.

**Circuit breaker + rate limit to the database.** Treat the database as a protected resource. When cache-miss traffic to it crosses a threshold, a circuit breaker trips and sheds or queues excess load — better to fail a slice of requests fast than to let the database collapse and fail *all* of them. A concurrency limiter on the rebuild path enforces the same bound.

**High availability for the cache itself.** Because a dead cache node is an avalanche trigger, run Redis in a replicated / clustered configuration so a single node failure doesn't drop the whole cache tier.

## The one-table summary

| Failure | Trigger | Who's affected | Primary defense | Secondary defense |
|---|---|---|---|---|
| **Penetration** | Key has no backing data | Non-existent keys | Cache the negative (short TTL) | Bloom-filter gate |
| **Breakdown** | One hot key expires | A single very hot key | Mutex / single-flight rebuild | Logical expiry + async refresh |
| **Avalanche** | Many keys expire together, or cache node dies | Broad swath of keys | TTL jitter | Multi-level cache, circuit breaker, HA |

The mental shortcut: if the interviewer's scenario involves keys that *don't exist*, it's penetration; if it's *one* famous key, it's breakdown; if it's *lots* of keys or the cache tier itself, it's avalanche. Name it correctly, then reach for the matching defense — and note aloud that jitter, negative caching, and a rebuild mutex are cheap enough that you'd apply them by default, not just after an incident.

**Try next:** the general cache-stampede treatment with probabilistic early expiration (XFetch) and singleflight coalescing at [/articles/microservices/2026-08-10-cache-stampede-request-coalescing](/articles/microservices/2026-08-10-cache-stampede-request-coalescing), and the dedicated bloom-filter article in this series for sizing the penetration gate.
