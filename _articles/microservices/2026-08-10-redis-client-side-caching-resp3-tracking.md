---
title: "Redis client-side caching: let the server invalidate your L1"
date: 2026-08-10
track: microservices
summary: "A near-cache in front of Redis is fast until it serves stale data. Redis's tracking feature flips the coherence problem around: the server remembers which keys you cached and pushes an invalidation over RESP3 the moment they change — no polling, no short TTLs. Here's how default tracking, BCAST prefixes, OPTIN/OPTOUT, and the RESP2 fallback actually work."
reading_time: 6
tags: [redis, client-side-caching, resp3, tracking, near-cache, invalidation]
sources:
  - title: "Redis — Client-side caching reference"
    url: "https://redis.io/docs/latest/develop/reference/client-side-caching/"
  - title: "Redis — CLIENT TRACKING command reference"
    url: "https://redis.io/docs/latest/commands/client-tracking/"
  - title: "Redis blog — Server-Assisted Client-Side Caching in Python (redis-py)"
    url: "https://redis.io/blog/redis-assisted-client-side-caching-in-python/"
  - title: "Lettuce — ClientSideCaching API (6.5.x)"
    url: "https://javadoc.io/static/io.lettuce/lettuce-core/6.5.4.RELEASE/io/lettuce/core/support/caching/ClientSideCaching.html"
  - title: "Redis blog — Faster Redis: client library support for client-side caching"
    url: "https://redis.io/blog/faster-redis-client-library-support-for-client-side-caching/"
---

You already have Redis as a shared L2 cache. But a network round-trip per lookup is still a round-trip, and for hot keys read thousands of times a second you'd rather keep a copy *in the application process* — an L1 near-cache. The moment you do that, you own the oldest problem in caching: how does the L1 know when its copy went stale?

The naive answers are all bad. Short TTLs mean you re-fetch constantly and still serve stale data inside the window. Polling for changes is chatty and lagged. What you actually want is for whoever changed the key to *tell you*. Redis's **tracking** feature does exactly that: the server remembers which keys a connection has read and sends it an **invalidation message** when one of them changes. This is "server-assisted client-side caching," and over RESP3 it arrives as an out-of-band push on the same connection you're already using.

(This article is about the Redis mechanism specifically. For the general L1/L2 near-cache pattern and its coherence trade-offs, see [multi-level caching and near-caches](/articles/sys-patterns/2026-08-10-multi-level-caching-near-cache).)

## Default (per-key) tracking

Turn it on per connection:

```
CLIENT TRACKING ON
```

Now, for every read-only command on that connection, the server records that this client may be caching the returned keys. It keeps this in a global **Invalidation Table** — a map from key to the set of client IDs that read it. It stores client *IDs*, not the keys themselves per client, which keeps memory bounded. The flow:

```
Client 1 -> Server: CLIENT TRACKING ON
Client 1 -> Server: GET foo          # server notes: client 1 cached "foo"
Client 2 -> Server: SET foo newval    # foo changed
Server  -> Client 1: invalidate "foo" # pushed, unsolicited
```

When `foo` is modified, evicted, or expires, every client in the table's entry for `foo` gets an invalidation and the entry is cleared. That "clear after notify" is deliberate: you get told **once**. After you re-read `foo`, you're tracked again. The table has a bounded size; under memory pressure Redis evicts entries by sending *fake* invalidations, so the worst case is an unnecessary L1 miss, never a stale read.

Over **RESP3** the invalidation is a `push` message on the same connection, so a single multiplexed connection both serves data and receives invalidations. This is why RESP3 matters — it's what makes tracking practical without a second socket.

## BCAST mode with prefixes

Per-key tracking costs server memory proportional to the number of distinct keys clients cache. If you have many clients caching many keys, that Invalidation Table gets expensive. **Broadcasting mode** trades that memory for a coarser signal:

```
CLIENT TRACKING ON BCAST PREFIX object: PREFIX user:
```

In BCAST mode the server stores **nothing per key**. It keeps only a **Prefixes Table** mapping each registered prefix to the clients subscribed to it. Any write to a key starting with `object:` or `user:` fans an invalidation out to every client on that prefix — *whether or not that client ever read the key*. You cache optimistically and eat some spurious invalidations in exchange for the server doing zero per-key bookkeeping.

Rules worth knowing:

- Prefixes **cannot overlap** — you can't register both `foo` and `foob`.
- With no `PREFIX`, the empty prefix is used and you get invalidations for **every** key in the keyspace. Rarely what you want.
- The full grammar (from the command reference) is:

```
CLIENT TRACKING <ON | OFF> [REDIRECT client-id]
  [PREFIX prefix [PREFIX prefix ...]] [BCAST] [OPTIN] [OPTOUT] [NOLOOP]
```

Use BCAST when your keys share a small set of namespaces and you'd rather burn a little client-side re-fetching than pay server memory. Use default tracking when caching is sparse and precise.

## OPTIN / OPTOUT: selective caching

By default (non-BCAST) every read is tracked. Often you only want to cache a subset — say, config keys, not per-request scratch data. Two opt modes:

**OPTIN** — nothing is tracked unless you ask, per command:

```
CLIENT TRACKING ON OPTIN
CLIENT CACHING YES
GET config:featureflags   # this read IS tracked
GET session:tmp:abc       # this read is NOT tracked
```

`CLIENT CACHING YES` applies to the single command that immediately follows (or, if that's `MULTI` or a Lua script, to the whole transaction/script).

**OPTOUT** — everything is tracked *except* what you exclude:

```
CLIENT TRACKING ON OPTOUT
CLIENT CACHING NO
GET counter:live          # not tracked (high churn, don't bother)
```

There's also **NOLOOP**: don't send me invalidations for keys *I* modified. Handy when the writer is also the reader and already knows to update its own L1. (Note: in default mode a tracked key you modify is still removed from the invalidation table even when NOLOOP suppresses the message to you.)

## The RESP2 fallback

Older clients stuck on RESP2 have no push type, so invalidations can't arrive inline. The workaround: **redirect** them to a second connection subscribed to a special Pub/Sub channel.

```
# Connection A (the invalidation sink): note its CLIENT ID, say 4
SUBSCRIBE __redis__:invalidate

# Connection B (the data connection): redirect invalidations to A
CLIENT TRACKING ON REDIRECT 4
GET foo
```

Now writes to `foo` deliver a normal Pub/Sub `message` on `__redis__:invalidate` to connection A, whose payload is the array of invalidated key names (a null message on `FLUSHALL`/`FLUSHDB`). It reuses the Pub/Sub *transport* but it is **not** a broadcast — only the redirected connection receives its own invalidations.

The catch, and the reason the RESP3 path is preferred: RESP2 Pub/Sub can silently drop messages, and if connection A dies you stop getting invalidations while B keeps happily reading. Redis's guidance is defensive: on invalidation-connection loss, **flush the entire L1 immediately**; periodically `PING` the invalidation channel and, if it goes quiet past a timeout, tear down and flush. Also put a **max TTL on every L1 entry** regardless of the source key's TTL, as a backstop against a missed invalidation.

## The read → invalidate race

Here's the subtle bug. With any tracking setup, this ordering is possible:

```
[data] client -> server: GET foo
[inval] server -> client: invalidate foo   # someone else just wrote it
[data] server -> client: "bar"             # the now-stale reply lands last
```

If you naively "cache the reply," you write the stale `bar` into your L1 *after* the invalidation that was meant to evict it — and it sticks forever. The fix, which good client libraries implement, is **invalidate-then-cache** with a placeholder:

```
cache["foo"] = CACHING_IN_PROGRESS   # before issuing GET
GET foo
# ... invalidation for foo arrives -> DELETE cache["foo"]
# ... GET reply "bar" arrives -> only store if cache["foo"] still present
```

Because the invalidation deleted the placeholder, the late reply sees no entry and declines to cache. You take a miss next time and re-fetch — correct, if slightly slower. Never resolve the race by keeping the value.

## Client-side flow

Putting it together as an L1 in front of Redis-as-L2:

```python
def get(key):
    v = l1.get(key)
    if v is not None:
        return v                    # L1 hit, zero round-trips
    v = redis.get(key)              # miss -> L2 (tracked read)
    l1.set(key, v, max_ttl=30)      # server will now invalidate on change
    return v

# separate listener thread on the RESP3 push / RESP2 invalidate channel:
def on_invalidate(keys):
    for k in (keys or l1.all_keys()):   # None => FLUSH-all sentinel
        l1.delete(k)
```

Real clients ship this for you. **redis-py** offers server-assisted caching when connected with `protocol=3` (RESP3), maintaining a local cache and wiring up invalidations; it falls back to the `__redis__:invalidate` Pub/Sub channel on RESP2 with the caveat that the RESP2 path is less reliable. **Lettuce** exposes `ClientSideCaching` / `CacheFrontend`, backed by tracking, letting you plug in your own near-cache map. Because it's a server feature, this also works on Valkey and against managed Redis where tracking is enabled.

The net effect: your L1 is as fresh as the network latency of a single push message, your L2 keeps absorbing misses, and you never chose a TTL as a guess about staleness again.

**Try next:** Start a local Redis, run `redis-cli -3` (RESP3), issue `CLIENT TRACKING ON`, `GET foo`, then from a second `redis-cli` do `SET foo bar` and watch the invalidation push land in the first session. Then compare `CLIENT TRACKINGINFO` output before and after switching to `BCAST PREFIX user:`.
