---
title: "Multi-Level Caching: The Near Cache and the Coherence Tax You Pay for Microseconds"
date: 2026-08-10
track: sys-patterns
summary: "A two-tier cache puts a microsecond-scale in-process L1 (Caffeine) in front of a large, shared L2 (Redis) — and the moment you do, every app instance holds its own private copy that a write on another node cannot reach. A concrete get/put through both tiers, why the coherence problem is the whole game, and the four ways people solve it: short L1 TTLs, pub/sub invalidation broadcast, versioned keys, and the fire-and-forget-plus-reconciliation model Hazelcast and Coherence ship as a 'near cache'."
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

You have a distributed cache — Redis, a network hop away, holding everything and consistent across all your app instances. Reads are already fast. Then someone profiles the hot path and finds that even a 0.5 ms Redis round-trip, multiplied by the number of times a request touches the cache, is the dominant cost. So they add a second cache: an in-process map (Caffeine, or a plain `ConcurrentHashMap`) that answers in *hundreds of nanoseconds* with no serialization and no socket. That is multi-level caching, and the local tier is what Hazelcast and Oracle Coherence call a **near cache**.

The speedup is real. The bill comes due immediately, and it is called coherence.

## Why two tiers at all

The two caches are good at opposite things, and that is the whole justification for running both.

- **L1 (in-process, e.g. Caffeine).** Sub-microsecond hits, zero network, zero deserialization. But it is *per-instance* — 20 app nodes means 20 independent L1 caches — and it is small, bounded by the heap you are willing to spend. It also vanishes on restart.
- **L2 (distributed, e.g. Redis).** One authoritative copy shared by every instance, large, survives app restarts, and consistent — a write is visible to all readers on the next `GET`. But every hit is a network round-trip and a deserialization.

Stacking them gives a hit-rate cascade. If L1 catches 90% of reads at ~200 ns and L2 catches most of the rest at ~0.5 ms, your average read latency and your Redis QPS both collapse. L1 absorbs the hottest keys — the ones read thousands of times a second — precisely the traffic that would otherwise pound L2. (Caffeine's window-TinyLFU admission policy, covered in its own article in this series, is what makes that small L1 punch above its size.)

## The read and write flow

The read path is a cascade; the write path is where the design decisions live.

**Read (get):** check L1 → on miss check L2 → on miss load from the database, then *backfill both tiers* on the way out.

**Write (put):** update the database, update or delete the key in L2, and then — the hard part — get rid of the now-stale copy sitting in every *other* node's L1.

```java
public class TwoTierCache<V> {
    private final Cache<String, V> l1;          // Caffeine, short TTL
    private final RedisCommands<String, V> l2;   // shared Redis (L2)
    private final Function<String, V> dbLoader;
    private final Duration l2Ttl;

    public V get(String key) {
        V v = l1.getIfPresent(key);              // L1: ~200 ns
        if (v != null) return v;

        v = l2.get(key);                         // L2: ~0.5 ms network hop
        if (v != null) {
            l1.put(key, v);                      // backfill L1
            return v;
        }

        v = dbLoader.apply(key);                 // origin
        if (v != null) {
            l2.setex(key, l2Ttl.getSeconds(), v);// backfill L2 with TTL
            l1.put(key, v);                      // backfill L1
        }
        return v;
    }

    public void put(String key, V value) {
        // origin write happens elsewhere (DB) — then update the tiers
        l2.setex(key, l2Ttl.getSeconds(), value);
        l1.put(key, value);                      // fresh on THIS node only
        publishInvalidation(key);                // tell the OTHER nodes
    }
}
```

Everything above is uncontroversial except the last line. Without it, the update you just made is invisible to L1 on all the other instances until their entries happen to expire.

## The hard problem: L1 caches drift apart

Here is the failure in one sentence: **node A writes key `user:42`, updates L2, and refreshes its own L1 — but nodes B, C, and D still hold the old `user:42` in their L1, and nothing has told them to drop it.** Until something does, a read on B returns stale data even though L2 is perfectly correct. This is the same cache-coherence problem CPU designers face between per-core caches, transplanted to a fleet of app servers over a network — except you get no hardware MESI protocol for free.

The distributed tier does not have this problem: it is single-copy and authoritative. The problem is *intrinsic to replicating data into many private L1 caches*. Every mitigation below is a different trade of freshness against traffic and complexity.

### 1. Short L1 TTLs (bound the staleness, do nothing else)

Give L1 entries a small time-to-live — say 5 seconds — and accept that a stale value can live at most that long. No messaging, no moving parts; you are simply capping the blast radius. This is the right default when the data tolerates brief staleness (product listings, config, counts) and wrong when it does not (balances, permissions). Note L1 and L2 TTLs are independent: L1 short to limit drift, L2 long to protect the origin.

### 2. Pub/sub invalidation broadcast (push a "drop this key" message)

On every write, publish the changed key to a channel every instance subscribes to; each subscriber evicts that key from its own L1. This is the mechanism in the OneUptime multi-node coherence write-up: *"When any node writes to the database, it publishes an invalidation event. All nodes (including the writer) subscribe and clear their local cache entry."*

```java
private static final String CHANNEL = "cache:invalidate";

void publishInvalidation(String key) {
    redis.publish(CHANNEL, key);            // fan-out to all instances
}

// One daemon subscriber per instance, started at boot:
redisPubSub.subscribe(new RedisPubSubListener() {
    public void message(String channel, String key) {
        l1.invalidate(key);                 // drop the local copy
    }
}, CHANNEL);
```

You need not publish it yourself. Enable Redis **keyspace notifications** (`CONFIG SET notify-keyspace-events KEA`) and Redis emits an event on every mutation to `__keyevent@0__:set` (carrying the key) and `__keyspace@0__:<key>` (carrying the event name) — subscribe and let writes invalidate themselves. Two caveats from the Redis docs decide whether this is safe for you: delivery is **fire-and-forget** — a subscriber that disconnects and reconnects *misses* events in the gap — and in a Redis Cluster **each node only emits events for its own keyspace**, so a single `psubscribe` will not see the whole cluster.

Redis's purpose-built version of this is **server-assisted client-side caching** (RESP3 tracking, its own article in this series). The server itself remembers which keys each client cached and pushes invalidations on `__redis__:invalidate`; `BCAST` mode instead broadcasts by key prefix at zero server memory. Same idea as your hand-rolled pub/sub, moved into the protocol — and it inherits the same fire-and-forget property, which is why the reference tells clients to **flush the entire local cache** if the invalidation connection ever drops.

### 3. Versioned keys (never invalidate — make staleness unnameable)

Instead of mutating `user:42`, write `user:42:v7` and bump a small, cheap-to-read version pointer. Readers resolve the current version first, so an old L1 entry keyed `user:42:v6` is simply never asked for again — it ages out harmlessly. This sidesteps the race where an invalidation message arrives *before* the fresh value is cached (the Redis client-side-caching reference calls this out explicitly and recommends a placeholder entry to defend against it). The cost is an extra indirection on read and cache space spent on dead versions until they evict.

### 4. The near-cache model: assume you'll miss events, then reconcile

Hazelcast and Coherence productize exactly this two-tier pattern and confront the fact that broadcasts get lost. With `invalidate-on-change` set to `true` (the default), a mutation evicts the near-cache entry cluster-wide. But — straight from the Hazelcast docs — *"invalidation events can be lost due to the fire-and-forget fashion of the eventing system,"* so they add two things a naive pub/sub scheme lacks:

- **Batching.** Invalidations are coalesced (`hazelcast.map.invalidation.batch.size`, default **100**; flushed at least every `hazelcast.map.invalidation.batchfrequency.seconds`, default **10 s**) so a write storm does not become a message storm.
- **Reconciliation / anti-entropy.** A periodic task (`hazelcast.invalidation.reconciliation.interval.seconds`, default **60 s**) compares invalidation-event sequence numbers each member *generated* against what each near cache *received*. If more than `hazelcast.invalidation.max.tolerated.miss.count` (default **10**) went missing, the stale data is made unreachable and the next `get` falls through to the authoritative map.

That combination — best-effort push for latency plus periodic reconciliation for correctness — is worth copying even in a DIY Redis setup: pub/sub for the common case, a short L1 TTL as the safety net that keeps drift bounded even when a message is lost.

## The stampede interaction

Multi-level caching changes the shape of cache stampede (the thundering-herd problem, covered under invalidation strategies elsewhere in this series). A broadcast invalidation is a coordinated eviction: the same hot key drops out of *every* L1 at nearly the same instant, and every node misses to L2 — or worse, to the database — simultaneously. Short synchronized L1 TTLs cause the same synchronized expiry. Defenses: add per-node TTL jitter so expiries scatter, and put a per-key load lock (single-flight) on the L1→L2→DB fill so only one thread per node repopulates while the rest wait. L1 actually *helps* here too — once one thread refills L1, the herd on *that* node is served locally.

## When L1 is worth it (and when it is a liability)

Add the local tier when reads vastly outnumber writes, a handful of hot keys dominate traffic, the values are expensive to deserialize, and the data tolerates seconds of staleness. Skip it when writes are frequent (you will spend more on invalidation chatter than you save), when the working set is uniform with no hot keys (low L1 hit rate, pure overhead), or when correctness forbids *any* stale read — in that last case, keep the single authoritative L2 and pay the network hop honestly. The near cache buys microseconds; the coherence machinery is the price, and you should only pay it where the microseconds actually matter.

**Try next:** enable `notify-keyspace-events KEA` on a scratch Redis, `psubscribe '__keyevent@0__:*'` in one terminal, and run writes in another — watch the events, then kill and restart the subscriber mid-write to see exactly which invalidations fire-and-forget drops on the floor.
