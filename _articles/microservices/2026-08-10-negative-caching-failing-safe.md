---
title: "Negative Caching and Failing Safe: Caching Absence, Errors, and Cache Outages"
date: 2026-08-10
track: microservices
summary: A cache that records only successes leaves two gaps — repeated misses for keys that will never exist, and the moment the cache tier itself becomes unreachable. This article covers caching absence with a sentinel and a short TTL, caching errors with stale-if-error, and choosing fail-open or fail-closed behaviour when the cache is gone.
reading_time: 7
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

**Gist.** A cache that stores only successful lookups converts two situations into origin load: queries for keys that do not exist, which miss forever, and the interval during which the cache tier itself is unreachable. Negative caching stores the *absence* of a result behind a distinct sentinel value with a deliberately short time-to-live (TTL), and fail-safe design fixes in advance whether an unavailable cache means "allow" or "deny". The cost is a window of incorrectness — a key created immediately after a negative entry is written reads as missing until that entry expires — plus the extra memory a negative entry occupies for every key an adversary can invent.

## The two gaps

The first gap is the lookup that returns nothing: a user identifier never issued, a product SKU that resolves to a 404. Each such request misses the cache, reaches the database, finds no row, and stores nothing, so an identical subsequent request repeats the full path. A client issuing a stream of non-existent keys therefore drives one origin query per request. This is **cache penetration**, covered separately in [cache penetration, breakdown, and avalanche](/articles/distributed-systems/2026-08-10-cache-penetration-breakdown-avalanche).

The second gap is failure of the cache tier itself — a Redis restart, a partition, or an avalanche across the whole tier. Code that lets "cache unreachable" propagate as an error turns a cache outage into a service outage. Code that silently falls through to the origin instead produces the opposite failure: every request that was previously a cache hit becomes a database query in the same instant.

## Negative caching: recording that nothing is there

The Domain Name System (DNS) has done this since [RFC 2308](https://www.rfc-editor.org/rfc/rfc2308). When a resolver asks for a name that does not exist, the authoritative server returns `NXDOMAIN`, and resolvers cache that non-existence answer rather than re-querying. **The TTL for the negative answer is taken from the `MINIMUM` field of the zone's start-of-authority (SOA) record, bounded by the TTL of the SOA record itself.** RFC 2308 states that "values of one to three hours have been found to work well" and that "values exceeding one day have been found to be problematic". A long negative TTL keeps a newly registered name invisible for the remainder of that interval.

Two design rules follow for application-level negative caching.

1. **The absent marker must be a sentinel value, not a missing key or an empty string.** A `GET` returning nil is ambiguous between "known to be absent" and "not cached"; a distinct sentinel removes the ambiguity, because the two cases require different handling — one is a hit, the other a miss.
2. **The negative TTL must be short**, on the order of seconds to a few minutes, because a negative entry is a prediction about the future. The key may be created immediately afterwards, and the entry's lifetime is exactly the window during which real data reads as missing.

A sentinel-based negative cache in Go over Redis:

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

Three properties carry the design. **The sentinel is non-empty**: an empty value or zero-length record collides with legitimately empty payloads, so the marker must be a byte sequence the real serialization cannot produce. **The negative TTL is much shorter than the positive one and carries jitter** — `jitter()` spreads expiry times so that a batch of negative entries written together does not expire together and re-stampede the origin. **Only "no such row" is cached**: a timeout or connection error is not evidence of absence, and storing a sentinel for it would hide a row that exists until the entry expires.

Against high-cardinality penetration — random keys that will never resolve — a negative cache still allocates one entry per distinct bogus key, so its memory grows with the attacker's key space. A **Bloom filter** placed in front bounds this: a negative answer from the filter is definitive, so the request is rejected before reaching Redis or the database. The negative cache then covers keys recently deleted, and the filter covers the adversarial stream.

## Caching errors: serving last-good while the origin fails

A failing origin is a different case from an absent row. Propagating the error and retrying against a struggling backend are both poor outcomes; a third option is to cache the error for a brief interval, or to continue serving the last known-good value.

HTTP formalises the latter in [RFC 5861](https://www.rfc-editor.org/rfc/rfc5861.html) as **`stale-if-error`**: "when an error is encountered, a cached stale response MAY be used to satisfy the request." Its companion `stale-while-revalidate` serves the stale response immediately and refreshes in the background. The application-level equivalent maintains **two expiry timestamps per entry, soft and hard**. Past the soft expiry a refresh is attempted; if the refresh fails, the stale value continues to be served until the hard expiry, at which point the entry is no longer usable and the error surfaces. The same stale-serving machinery appears in [cache stampede](/articles/microservices/2026-08-10-cache-stampede-request-coalescing), there for throughput rather than resilience.

Where an error response itself must be cached — a 429 or a transient 503 with no stale value available — it is cached with a **very short TTL**, on the order of seconds. A response determined by the request rather than by origin state — a malformed body, a failed authorization — is not shared-cacheable at all, which is a separate question from caching the absence of a resource. The constraint is the same as for negative caching: a transient failure must not become a durable answer.

### Implementation sketch (Scala)

The soft/hard expiry state machine, with the origin call as the only failure point:

```scala
final case class Entry[A](value: A, soft: Long, hard: Long)

enum Result[+A]:
  case Fresh(value: A)
  case Stale(value: A)   // origin failed, within hard expiry
  case Failed(cause: Throwable)

final class StaleIfError[K, V](
    softTtlMs: Long,
    hardTtlMs: Long,
    load: K => V
):
  private val entries = scala.collection.concurrent.TrieMap.empty[K, Entry[V]]

  def get(key: K, now: Long): Result[V] = entries.get(key) match
    case Some(e) if now < e.soft => Result.Fresh(e.value)
    case Some(e) if now < e.hard => refresh(key, now).getOrElse(Result.Stale(e.value))
    case _                       => refresh(key, now).getOrElse(Result.Failed(Expired))

  // A failed refresh must not evict: the stale value is the fallback.
  private def refresh(key: K, now: Long): Option[Result[V]] =
    try
      val v = load(key)
      entries.update(key, Entry(v, now + softTtlMs, now + hardTtlMs))
      Some(Result.Fresh(v))
    catch case _: Exception => None

  private val Expired = RuntimeException("no usable entry")
```

The load-bearing line is the `catch` that returns `None` without touching `entries`: eviction on refresh failure would discard the value that `stale-if-error` exists to serve.

## Failing safe when the cache is unreachable

For the cache tier being down, the common default posture is **fail-open** — a cache error is treated as a miss and the read proceeds to the database, so an outage of the cache degrades latency rather than availability. Unqualified fail-open has a failure mode of its own: a request rate previously absorbed by cache hits arrives at the database in full when the cache disappears, which is a **cache avalanche**. Fail-open without a governor relocates the outage rather than preventing it.

The fallback path is therefore wrapped in a **circuit breaker** and a **concurrency limit**. The breaker stops repeated attempts against an unresponsive cache once failures accumulate; the limiter caps how many fallback queries reach the database concurrently, so the fallback cannot itself become the stampede. The breaker state machine is described in [circuit breakers with Resilience4j](/articles/microservices/2026-07-24-circuit-breakers-resilience4j).

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

The choice between postures follows from what the cached value decides. **Fail-open suits read-mostly, non-authoritative caches** — product pages, profiles, feeds — where a slower or staler answer is preferable to an error. **Fail-closed is required where the cache is authoritative for a safety decision**: rate-limiter counters held in Redis, a token denylist, a paywall check. If such a control fails open, disabling the cache is sufficient to bypass the control. The question a review must answer is whether, when the cache returns nothing, the safe answer is to allow the request or to reject it. Negative caching determines how absence is recorded; fail-safe design determines what absence means once the cache can no longer report it.

## Pitfalls

- **An empty string or empty struct used as the absent marker collides with legitimate empty payloads**, so a real record whose serialized form is empty is reported as missing. The marker must be a byte sequence the serializer cannot emit.
- **Caching a timeout or connection error as a negative entry hides existing data** for the whole negative TTL, because the code treated "could not determine" as "determined to be absent".
- **A negative TTL set as long as the positive TTL turns creation into a delayed-visibility bug**: a key written moments after the sentinel is invisible until the sentinel expires, with no write path invalidating it.
- **Negative entries created in one burst and given identical TTLs expire simultaneously**, producing a second stampede at expiry. Jitter on the TTL is what separates them.
- **A negative cache alone does not bound memory under a penetration attack**, since each distinct invented key allocates its own entry; the bound comes from a membership filter in front of the cache.
- **Fail-open without a concurrency limit converts a cache outage into a database outage**, because the entire hit rate arrives at the origin at once.
- **Fail-open on an authoritative cache removes the control it enforces**: with rate-limiter counters or a denylist in Redis, making Redis unavailable is equivalent to disabling the check.
- **Evicting a cache entry when its refresh fails destroys the value `stale-if-error` would have served**, converting a recoverable degradation into an error response.
- **Caching a request-specific rejection — a malformed body, a failed authorization — under a key shared across clients** makes one client's mistake visible to the others for the TTL, since the response depended on the request, not on origin state.
