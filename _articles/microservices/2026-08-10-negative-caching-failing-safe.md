---
title: "Negative Caching and Failing Safe: Caching Absence, Errors, and Cache Outages"
date: 2026-08-10
track: microservices
summary: A cache that only remembers successes leaves two gaps attackers and outages walk straight through — repeated misses for keys that will never exist, and the moment the cache itself dies. Here is how to cache absence with a sentinel and short TTL, cache errors with stale-if-error, and fail safe when the cache is gone.
reading_time: 6
tags:
  - caching
  - resilience
  - reliability
  - go
  - redis
sources:
  - title: "RFC 2308 — Negative Caching of DNS Queries (DNS NCACHE)"
    url: "https://www.rfc-editor.org/rfc/rfc2308"
  - title: "RFC 5861 — HTTP Cache-Control Extensions for Stale Content (stale-if-error)"
    url: "https://www.rfc-editor.org/rfc/rfc5861.html"
  - title: "How to Implement Negative Caching in Redis (OneUptime)"
    url: "https://oneuptime.com/blog/post/2026-03-31-redis-negative-caching/view"
  - title: "Redis + Circuit Breaker: The Real Defense Against Database Meltdowns (DEV Community)"
    url: "https://dev.to/tpmsh/redis-circuit-breaker-the-real-defense-against-database-meltdowns-in-high-throughput-systems-2a9o"
  - title: "Cache Failure Modes: Penetration, Avalanche, and Stampede (System Design School)"
    url: "https://systemdesignschool.io/fundamentals/cache-failure-modes"
---

Most caching write-ups optimise the happy path: a value exists, you store it, you serve it fast. But a cache that only remembers successes has two blind spots, and both turn into outages. The first is the lookup that returns *nothing* — a user ID that was never issued, a product SKU that 404s. Every one of those requests misses the cache, falls through to the database, finds nothing, and stores nothing, so the next identical request repeats the whole trip. An attacker who spams non-existent keys turns your cache into a passthrough and your database into the bottleneck. This is **cache penetration** (covered separately in [cache penetration, breakdown, and avalanche](/articles/distributed-systems/2026-08-10-cache-penetration-breakdown-avalanche)).

The second blind spot is the cache *itself* failing. Redis restarts, a network partition isolates it, or the whole tier avalanches — and if your code treats "cache unreachable" as an exception that bubbles up, you have converted a cache outage into a total outage. Handle it the other way and you get a stampede: every request now goes to the database at once.

Negative caching and fail-safe design address exactly these two gaps: cache the *absence* of a result, cache *errors* deliberately, and decide in advance how the system behaves when the cache is gone.

## Negative caching: remember that nothing is there

The idea is old and battle-tested. DNS has done it since [RFC 2308](https://www.rfc-editor.org/rfc/rfc2308): when a resolver asks for a name that does not exist, the authoritative server returns `NXDOMAIN`, and resolvers cache that "does not exist" answer so they stop re-asking. The TTL for the negative answer is taken from the zone's `SOA.MINIMUM` field (bounded by the SOA record's own TTL). RFC 2308 is explicit about keeping it short: "Values of one to three hours have been found to work well... Values exceeding one day have been found to be problematic." A too-long negative TTL means a name you just registered stays invisible for hours.

That gives us the two design rules for application-level negative caching:

1. **Use a sentinel value**, not an empty string or a missing key, so you can tell "known to be absent" apart from "not cached." A plain `GET` that returns nil is ambiguous — it could mean either. A distinct sentinel removes the ambiguity.
2. **Use a short TTL** — seconds to a couple of minutes — because a negative entry is a guess about the future. The key might get created a moment later, and a long negative TTL becomes a correctness bug where real data reads as missing.

Here is a sentinel-based negative cache in Go over Redis:

```go
const nullSentinel = "\x00NULL\x00" // distinct from any real serialized value

var (
	ErrNotFound = errors.New("not found")
	posTTL      = 10 * time.Minute
	negTTL      = 30 * time.Second // deliberately short
)

func GetUser(ctx context.Context, rdb *redis.Client, db *DB, id string) (*User, error) {
	key := "user:" + id

	switch val, err := rdb.Get(ctx, key).Result(); {
	case err == nil && val == nullSentinel:
		return nil, ErrNotFound // known-absent, cached hit — no DB trip
	case err == nil:
		return decodeUser(val)
	case err != redis.Nil:
		// cache error, not a miss — handled in the next section
		return getUserFailSafe(ctx, db, id)
	}

	// genuine miss: consult the database
	user, err := db.LoadUser(ctx, id)
	if errors.Is(err, sql.ErrNoRows) {
		// cache the ABSENCE so the next lookup is a hit
		rdb.Set(ctx, key, nullSentinel, jitter(negTTL))
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err // real DB error: do NOT cache
	}

	rdb.Set(ctx, key, encodeUser(user), jitter(posTTL))
	return user, nil
}
```

Three details are interview-grade. **The sentinel is not empty** — an empty value or a zero-length record collides with legitimately empty payloads, so pick a byte sequence that cannot appear in your real serialization. **The negative TTL is much shorter than the positive one** and carries jitter (`jitter()` spreads expiries so a batch of negatives created together does not expire together and re-stampede). **Only "no such row" gets cached** — a timeout or connection error is *not* an absence, so caching it as a sentinel would hide data that actually exists.

For high-cardinality penetration attacks — random keys that will never resolve — a negative cache still stores one entry per bogus key and can be memory-flooded. Pair it with a **Bloom filter** in front: if the filter says "definitely not present," reject before touching Redis or the DB at all. The negative cache handles the churn of legitimately-recently-deleted keys; the Bloom filter handles the adversarial firehose.

## Caching errors: serve last-good when the origin is down

Absence is one thing; a failing origin is another. If a downstream service returns 5xx, you have two bad options — propagate the error, or hammer the struggling backend with retries. A third option is to cache the error briefly, or better, keep serving the last good value.

HTTP formalised this in [RFC 5861](https://www.rfc-editor.org/rfc/rfc5861.html) with **`stale-if-error`**: "when an error is encountered, a cached stale response MAY be used to satisfy the request." Its sibling `stale-while-revalidate` serves stale immediately and refreshes in the background. The application-level equivalent: keep a *soft* expiry and a *hard* expiry on cached values. Past soft expiry you try to refresh; if the refresh fails, you serve the stale value until hard expiry rather than erroring. (This is the same stale-serving machinery discussed in [cache stampede](/articles/microservices/2026-08-10-cache-stampede-request-coalescing) — reused here for resilience rather than throughput.)

If you must cache an actual error response (say a 429 or a transient 503 you cannot serve stale for), cache it with a **very short TTL** — a few seconds — and never cache 4xx client errors as if they were server state. The rule mirrors negative caching: short TTL, explicit sentinel, and never let a transient failure become a durable lie.

## Failing safe when the cache dies

Now the hard case: the cache tier itself is unreachable. The default posture should be **fail-open** — treat a cache error as a miss and read straight from the database, so a Redis blip degrades latency instead of availability. But naive fail-open is a trap. If 50,000 req/s were all being served from cache and the cache vanishes, all 50,000 now hit the database simultaneously — a **cache avalanche**. Fail-open without a governor just moves the outage.

The fix is to wrap the fallback in a **circuit breaker** and a concurrency limit. When cache reads start failing, the breaker opens and you stop pounding a dead cache; the limiter caps how much load reaches the database so the fallback path cannot itself become the stampede. (See [circuit breakers with Resilience4j](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) for the state machine.)

```go
// getUserFailSafe runs when the cache read errored (cache down, not a miss).
func getUserFailSafe(ctx context.Context, db *DB, id string) (*User, error) {
	if !dbBreaker.Allow() {
		// breaker open: shed load rather than avalanche the DB
		return nil, ErrServiceBusy
	}
	// limit concurrent DB fallbacks so a cache outage can't stampede
	if err := dbLimiter.Acquire(ctx); err != nil {
		return nil, ErrServiceBusy
	}
	defer dbLimiter.Release()

	user, err := db.LoadUser(ctx, id)
	if err != nil {
		dbBreaker.RecordFailure()
		return nil, err
	}
	dbBreaker.RecordSuccess()
	return user, nil
}
```

**Fail-open is the right default for read-mostly, non-authoritative caches** — product pages, profiles, feeds — where a stale or slower answer beats an error. **Fail-closed is correct when the cache is authoritative for a safety decision**: a rate limiter whose counters live in Redis, a token denylist, a paywall check. If those "fail open," an attacker just needs to knock over Redis to bypass the control entirely. The question to ask in review is always: *if the cache returns nothing, is the safe answer "let it through" or "keep it out"?* Negative caching decides how you remember absence; fail-safe design decides what absence means when the cache can no longer tell you.

**Try next:** Add a Bloom filter in front of `GetUser` and measure how many DB round-trips it eliminates under a synthetic penetration attack of random non-existent IDs; then kill Redis mid-load and confirm your breaker opens before your database connection pool saturates.
