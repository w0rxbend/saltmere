---
title: "Multi-Level Caching: The Near Cache and the Coherence Tax Paid for Microseconds"
date: 2026-08-10
track: sys-patterns
summary: "A two-tier cache places a microsecond-scale in-process L1 (Caffeine) in front of a large shared L2 (Redis); from that moment every application instance holds a private copy that a write on another node cannot reach. A concrete get/put through both tiers, why coherence is the whole problem, and four mitigations: short L1 TTLs, pub/sub invalidation broadcast, versioned keys, and the fire-and-forget-plus-reconciliation model shipped by Hazelcast and Oracle Coherence as a near cache."
reading_time: 7
tags: [caching, near-cache, redis, caffeine, hazelcast, pub-sub, cache-coherence, invalidation]
sources:
  - title: "Near Cache — Hazelcast Documentation 5.7 (invalidate-on-change, batching, reconciliation)"
    url: "https://docs.hazelcast.com/hazelcast/5.7/cluster-performance/near-cache"
  - title: "Redis keyspace notifications — Redis Docs (notify-keyspace-events, __keyspace@/__keyevent@ channels)"
    url: "https://redis.io/docs/latest/develop/pubsub/keyspace-notifications/"
  - title: "Client-side caching reference — Redis Docs (server-assisted tracking, BCAST, __redis__:invalidate)"
    url: "https://redis.io/docs/latest/develop/reference/client-side-caching/"
  - title: "How to Implement Cache Coherence in Multi-Node Systems with Redis — OneUptime Blog"
    url: "https://oneuptime.com/blog/post/2026-03-31-redis-cache-coherence-multi-node/view"
---

**Gist.** A distributed cache such as Redis answers over a socket in sub-millisecond time, and on a hot path that touches the cache many times per request the accumulated round-trips dominate. Multi-level caching adds an in-process first tier — a **near cache**, in Hazelcast and Oracle Coherence terminology — that answers in hundreds of nanoseconds with no socket and no deserialization. The cost is coherence: each instance now holds a private replica, and a write performed on one node leaves stale copies on every other node that no mechanism removes for free.

## Why two tiers

The two tiers have complementary properties, and that complementarity is the entire justification for running both.

- **L1 (in-process, e.g. Caffeine).** Sub-microsecond hits, no network, no deserialization. It is **per-instance** — twenty application nodes mean twenty independent L1 caches — bounded by the heap allocated to it, and lost on restart.
- **L2 (distributed, e.g. Redis).** One authoritative copy shared by every instance, large, surviving application restarts, and **single-copy** for a given key: a write to the primary is visible to every reader that issues its next `GET` against that primary. Every hit costs a network round-trip and a deserialization.

Stacking them produces a hit-rate cascade: whatever fraction of reads L1 absorbs is removed from both the mean read latency and the L2 query rate, because those reads never leave the process. L1 absorbs the hottest keys — the ones read most often — which is precisely the traffic that would otherwise concentrate on L2. Caffeine's window-TinyLFU admission policy, covered separately in this series, is aimed at exactly that skew: retaining the frequently requested entries in a small cache.

## Read and write paths

The read path is a cascade; the write path is where the design decisions live.

**Read (get):** consult L1; on miss consult L2; on miss load from the origin database, then **backfill both tiers** on the way out.

**Write (put):** write the origin, update or delete the key in L2, and then remove the now-stale copy held in every *other* node's L1. Only the last step is contentious. Without it, the update is invisible to L1 on all other instances until their entries expire.

### Implementation sketch (Scala)

The L2 and the broadcast channel are declared as local traits so the sketch commits to no particular client library; `l1` is a Caffeine cache used through its Java API.

```scala
trait L2[V]:
  def get(key: String): Option[V]
  def setex(key: String, ttl: Duration, value: V): Unit

trait Invalidations:
  def publish(key: String): Unit
  def subscribe(onKey: String => Unit): Unit

final class TwoTierCache[V](
    l1: com.github.benmanes.caffeine.cache.Cache[String, V], // short TTL
    l2: L2[V],
    bus: Invalidations,
    load: String => Option[V],
    l2Ttl: Duration
):
  bus.subscribe(l1.invalidate)   // drop the local copy on any node's write

  def get(key: String): Option[V] =
    Option(l1.getIfPresent(key)) // in-process, no socket
      .orElse:
        l2.get(key).map: v =>
          l1.put(key, v)         // backfill L1 only
          v
      .orElse:
        load(key).map: v =>
          l2.setex(key, l2Ttl, v)
          l1.put(key, v)         // backfill both tiers
          v

  def put(key: String, value: V): Unit =
    l2.setex(key, l2Ttl, value)
    l1.put(key, value)           // fresh on this node only
    bus.publish(key)             // every other node must be told
```

## The coherence failure

The failure states in one sentence: **node A writes `user:42`, updates L2 and refreshes its own L1, while nodes B, C and D still hold the previous `user:42` in their L1 and have received no instruction to discard it.** Until something instructs them, a read on B returns stale data even though L2 is correct. This is the cache-coherence problem that CPU designers face between per-core caches, relocated to a fleet of application servers connected by a network, without a hardware coherence protocol underneath.

The distributed tier does not exhibit the problem, because it is single-copy and authoritative. The problem is **intrinsic to replicating data into many private L1 caches**. Each mitigation below trades freshness against message traffic and complexity.

### 1. Short L1 TTLs

Assign L1 entries a small time-to-live and accept that a stale value can persist for at most that interval. No messaging is involved; the bound on staleness is the only guarantee. This is appropriate where the data tolerates brief staleness and inappropriate where it does not, such as balances and permissions. **L1 and L2 TTLs are independent**: L1 short to bound drift, L2 long to shield the origin.

### 2. Pub/sub invalidation broadcast

On every write, publish the changed key to a channel to which every instance subscribes; each subscriber evicts that key from its own L1. This is the mechanism described in the OneUptime multi-node coherence write-up: *"When any node writes to the database, it publishes an invalidation event. All nodes (including the writer) subscribe and clear their local cache entry."*

Publication need not be explicit. With Redis **keyspace notifications** enabled (`CONFIG SET notify-keyspace-events KEA`), Redis emits an event on every mutation to `__keyevent@0__:set`, carrying the key, and to `__keyspace@0__:<key>`, carrying the event name. Two properties documented by Redis determine whether this is safe for a given deployment: delivery is **fire-and-forget**, so a subscriber that disconnects and reconnects **misses every event in the gap**; and in Redis Cluster **each node emits events only for its own keyspace**, so a single `psubscribe` does not observe the whole cluster.

Redis's purpose-built form of this is **server-assisted client-side caching** over RESP3 tracking, treated in its own article in this series. The server records which keys each client has cached and pushes invalidations on `__redis__:invalidate`; `BCAST` mode instead broadcasts by key prefix, holding no per-client key state on the server. It inherits the same fire-and-forget property, which is why the reference directs clients to **flush the entire local cache** whenever the invalidation connection drops.

### 3. Versioned keys

Rather than mutating `user:42`, write `user:42:v7` and advance a small version pointer that readers resolve first. An L1 entry keyed `user:42:v6` is then never requested again and ages out without harm. This avoids the race in which an invalidation message arrives *before* the fresh value has been cached; the Redis client-side-caching reference identifies that race and recommends caching a placeholder entry to defend against it. The cost is one extra indirection per read plus the cache space occupied by dead versions until eviction.

### 4. The near-cache model: assume events are lost, then reconcile

Hazelcast and Coherence productise this two-tier pattern and treat lost broadcasts as expected. With `invalidate-on-change` set to `true`, the default, a mutation evicts the near-cache entry cluster-wide. The Hazelcast documentation states that *"invalidation events can be lost due to the fire-and-forget fashion of the eventing system,"* and adds two mechanisms a plain pub/sub scheme lacks:

- **Batching.** Invalidations are coalesced (`hazelcast.map.invalidation.batch.size`, default **100**; flushed at least every `hazelcast.map.invalidation.batchfrequency.seconds`, default **10 s**), so a write storm does not become a message storm.
- **Reconciliation (anti-entropy).** A periodic task (`hazelcast.invalidation.reconciliation.interval.seconds`, default **60 s**) compares the invalidation-event sequence numbers each member *generated* against those each near cache *received*. If more than `hazelcast.invalidation.max.tolerated.miss.count` (default **10**) are missing, the stale data is made unreachable and the next `get` falls through to the authoritative map.

The combination — best-effort push for latency, periodic reconciliation for correctness — transfers to a hand-built Redis arrangement: pub/sub for the common case, and a short L1 TTL as the bound that holds when a message is lost.

## Interaction with stampede

Multi-level caching alters the shape of cache stampede, the thundering-herd problem covered under invalidation strategies elsewhere in this series. **A broadcast invalidation is a coordinated eviction**: the same hot key leaves every L1 at nearly the same instant, and every node then misses to L2, or to the database. Synchronised short L1 TTLs produce the same synchronised expiry. Two defences apply: per-node TTL jitter, so expiries scatter; and a per-key load lock (single-flight) over the L1 → L2 → origin fill, so one thread per node repopulates while the rest wait. L1 also reduces the herd, since once one thread refills L1 the remaining requests on that node are served locally.

## When the local tier is justified

The local tier pays where reads greatly outnumber writes, a small set of hot keys dominates traffic, values are expensive to deserialize, and the data tolerates seconds of staleness. It is a liability where writes are frequent, since invalidation traffic then exceeds the saving; where the working set is uniform with no hot keys, giving a low L1 hit rate and pure overhead; and where correctness forbids any stale read, in which case the single authoritative L2 and its network hop remain the correct design. The near cache buys microseconds, and the coherence machinery is the price of those microseconds.

## Pitfalls

- **A restarted or reconnected subscriber silently serves stale data.** Redis pub/sub and keyspace notifications are fire-and-forget: events emitted during the disconnection are never redelivered, so the local L1 retains entries whose invalidations were dropped. The Redis client-side-caching reference prescribes flushing the entire local cache on reconnection.
- **A single `psubscribe` misses most invalidations in Redis Cluster.** Each cluster node emits keyspace events only for the keys it owns, so a subscriber connected to one node observes only that node's share of the keyspace.
- **An invalidation that arrives before the fresh value is cached leaves the stale value cached until its TTL expires.** The eviction removes an entry that has not yet been rewritten, and the subsequent fill stores the value read before the write, which no further invalidation will arrive to remove; caching a placeholder entry closes the window.
- **Broadcast invalidation of a hot key produces a synchronised fleet-wide miss.** Every L1 drops the entry at the same instant and every node refills concurrently, converting one write into a burst against L2 or the origin; TTL jitter and per-key single-flight bound the burst.
- **A long L1 TTL turns a lost invalidation into a long-lived incorrect read.** The TTL is the only bound on staleness once a message is lost, so its length is the worst-case staleness of the system regardless of how reliable the broadcast normally appears.
- **Writes that bypass the application's `put` path never invalidate anything.** Administrative scripts, batch jobs and direct database updates leave every L1 holding values the write path would have evicted; keyspace notifications catch the subset of those writes that pass through Redis, and none of the ones that do not.
