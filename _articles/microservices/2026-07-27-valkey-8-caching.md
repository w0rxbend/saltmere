---
title: "Valkey 8 as a Cache Layer: The Redis Fork, and a Cache-Aside That Does Not Stampede"
date: 2026-07-27
track: microservices
summary: "Why Valkey forked from Redis in 2024, what 8.0 and 8.1 changed in the engine, and a cache-aside pattern with TTLs and stampede protection."
reading_time: 7
tags: [valkey, caching, redis, microservices, cache-aside]
sources:
  - title: "Linux Foundation Launches Open Source Valkey Community"
    url: "https://www.linuxfoundation.org/press/linux-foundation-launches-open-source-valkey-community"
  - title: "Announcing Valkey 8.0 (Linux Foundation)"
    url: "https://www.linuxfoundation.org/press/valkey-8-0"
  - title: "Valkey 8.0: Delivering Enhanced Performance and Reliability"
    url: "https://valkey.io/blog/valkey-8-0-0-rc1/"
  - title: "Valkey 8.1: Continuing to Deliver Enhanced Performance and Reliability"
    url: "https://valkey.io/blog/valkey-8-1-0-ga/"
  - title: "Valkey Releases"
    url: "https://valkey.io/download/releases/"
---

**Gist.** A read-heavy microservice fronts its database with an in-memory cache, but in March 2024 the default choice for that cache — Redis — moved off the permissive BSD licence, and the cache-aside pattern itself has a failure mode in which every concurrent reader of an expired key reaches the database at once. Valkey, the Linux Foundation fork of Redis 7.2.4, preserves the BSD 3-Clause licence and the RESP (REdis Serialization Protocol) wire format, while its 8.x line adds I/O threading and a lower per-key memory footprint; the stampede is contained by admitting a single recomputing caller per key and by randomising recompute times before expiry. The cost is that both mitigations trade freshness and code complexity for tail latency: losers of the recompute race are served a stale value, and every cached entry carries extra bookkeeping fields.

## The licence change and the fork

Redis Inc. relicensed the Redis server from BSD to a dual RSALv2 / SSPLv1 model, neither of which is approved by the Open Source Initiative (OSI). On **March 28, 2024 the Linux Foundation announced Valkey**, a fork of **Redis 7.2.4** under the permissive **BSD 3-Clause** licence, with backing from AWS, Google Cloud, Oracle and Ericsson among others.

For a cache layer the migration surface is small. Valkey speaks the same RESP protocol and the same command set as the version it forked from, so existing clients, connection pools and key schemas continue to work without modification. Divergence accumulates only in the direction of features added after the fork.

## Engine changes in the 8.x line

**Valkey 8.0** (announced generally available by the Linux Foundation in September 2024) is organised around **asynchronous I/O threading**. Rather than confining all work to a single-threaded event loop, 8.0 spreads command processing and I/O across cores and batches commands, which improves memory access locality. The project reports **roughly 1.2 million requests per second on an AWS `r7g` instance, around three times what it measured for the 7.2 line on the same hardware**.

Two 8.0 changes reduce memory. Keys are **embedded directly in the main dictionary**, removing a separate key pointer and cutting memory by **roughly 10% on the workloads the project measured**. Per-slot linked lists are replaced by a **per-slot dictionary**. Replication gains **dual-channel RDB (Redis Database file) synchronisation**, so a full synchronisation no longer competes with the replication backlog.

**Valkey 8.1**, released in 2025, continues the efficiency work. A new hashtable implementation reduces allocations per object, **saving roughly 20 bytes per key–value pair**, with a further saving when a TTL (time to live) is set. At tens of millions of small entries this is the difference between fitting an instance and not. Iterator prefetching speeds up key iteration, which affects `KEYS`-style scans and replica synchronisation. TLS (Transport Layer Security) connection negotiation is offloaded to the I/O thread pool, raising the rate at which new connections can be accepted. Sorted-set rank lookups (`ZRANK`) are faster as well, which matters for leaderboard reads. The project publishes its own measured multipliers for each of these; they are workload-specific and no independent benchmark reproduces them.

8.1 also adds a cache primitive that removes a scripting dependency: **`SET key val IFEQ oldval`**, a conditional write that applies only when the current value equals `oldval`. This is a compare-and-set executed by the server, where previously the same atomicity required a Lua script.

The 8.1 line continues to receive patch releases; the release list on the project site is the only current record of which patch is newest.

## Running it

The container image is a drop-in replacement on the default port.

```bash
docker run --name cache -p 6379:6379 -d valkey/valkey:8.1
valkey-cli SET session:42 "{...}" EX 300      # value with a 5-minute TTL
valkey-cli TTL session:42                       # -> (integer) 297
```

An existing Redis client pointed at `localhost:6379` connects without change.

## Cache-aside and the stampede

Cache-aside, also called lazy loading, is the standard read path: read the cache; on a miss, load from the source of truth, write the result back with a TTL, and return it. The invariant is that **the cache is never the source of truth** — every entry is reconstructible from the database, so eviction and loss are correctness-preserving.

The failure mode is the **cache stampede**, also called the thundering herd. A key with high request concurrency expires at a single instant. Every in-flight request observes a miss, and all of them issue the same database query. The database sees a load spike proportional to the request concurrency on that one key rather than to the miss rate, and the resulting latency can extend the recompute window, which admits still more missing readers.

Two mitigations compose:

- **Single-flight recomputation.** A caller attempts `SET lock:<key> 1 NX EX 10`. `NX` makes the write succeed only if the lock key is absent, so **exactly one caller wins per expiry event**; `EX` bounds the lock's lifetime, so a worker that crashes mid-recompute cannot wedge the key permanently. Losers do not queue on the database.
- **Probabilistic early expiration (XFetch).** Each entry records its absolute expiry and `delta`, the wall-clock duration the last database load took. A reader recomputes when `now − delta · β · ln(U) ≥ expiry`, with `U` drawn uniformly from (0, 1). Because `ln(U)` is negative, the term shifts the effective deadline earlier by a random amount scaled by how expensive the recompute is. **Expensive entries are refreshed earlier and with wider spread; cheap entries are refreshed close to expiry.** The recompute times of independent readers therefore do not coincide.

The two mitigations address different halves of the problem. Early expiration reduces the probability that a stampede is triggered at all; the lock bounds the damage when one is.

The winner deletes the lock key as soon as the recompute finishes, which is why the `EX 10` bound covers only one case: a process that dies before it can delete. For a read path, serving the losers a value that is at most one TTL stale is preferable to serving them a timeout.

### Implementation sketch (Scala)

The load-bearing part is the predicate, which is pure and testable independently of any client.

```scala
import scala.util.Random

final case class Entry[A](value: A, expiryMs: Long, deltaMs: Long)

/** XFetch: true when this reader should recompute now.
  * ln(U) < 0 for U in (0,1), so the term moves the deadline earlier by a
  * random amount proportional to the cost of the last recompute. */
def shouldRecompute[A](e: Entry[A], nowMs: Long, beta: Double = 1.0): Boolean =
  val u = math.max(Random.nextDouble(), Double.MinPositiveValue)
  nowMs - (e.deltaMs * beta * math.log(u)) >= e.expiryMs

/** Single-flight: only the caller that wins `SET NX EX` touches the source. */
def readThrough[A](
    key: String,
    get: String => Option[Entry[A]],
    setNx: (String, Long) => Boolean,   // returns true for the single winner
    put: (String, Entry[A]) => Unit,
    load: () => A,
    nowMs: () => Long,
    ttlMs: Long
): Option[A] =
  val cached = get(key)
  cached match
    case Some(e) if !shouldRecompute(e, nowMs()) => Some(e.value)
    case _ =>
      if setNx(s"lock:$key", 10_000L) then
        val t0 = nowMs()
        val v = load()
        val now = nowMs()
        put(key, Entry(v, now + ttlMs, now - t0))
        Some(v)
      else cached.map(_.value)   // loser serves stale rather than blocking
```

`readThrough` takes the cache operations as functions so the state machine — fresh, early-refresh, miss, lock lost — can be exercised with a deterministic clock and a forced `setNx` outcome.

## Pitfalls

- **A `delta` of zero disables early expiration.** If the recompute is fast enough to round to zero milliseconds, `delta · β · ln(U)` is zero and every reader recomputes exactly at expiry — the stampede returns for the cheapest keys, which are the ones most likely to be hot.
- **Releasing a lock the caller no longer owns.** A recompute that exceeds the lock's `EX 10` window lets a second caller acquire the same lock; the first caller's `DELETE` then removes the second caller's lock, admitting a third. Deleting only when the lock value matches a caller-unique token avoids this, and 8.1's `SET ... IFEQ` provides the matching compare on the write side.
- **Storing the expiry only in the server's TTL.** XFetch needs the absolute expiry and `delta` inside the value; a key whose TTL has already elapsed is gone, so the heuristic cannot read anything from it.
- **Assuming version parity with Redis after the fork.** Valkey forked at Redis 7.2.4; commands added on either side since then exist on one only. `SET ... IFEQ` is a Valkey 8.1 addition.
- **Reading the 1.2 M requests-per-second figure as a workload guarantee.** It is a project-reported number on an AWS `r7g` instance, not a bound for arbitrary command mixes, value sizes or TLS-terminated connections.
- **A loser with nothing to serve.** When the key is absent entirely, there is no stale value to fall back on; a retry loop there needs its own attempt cap, or the retry depth grows with the duration of the source-of-truth outage.
