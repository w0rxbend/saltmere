---
title: "Redis client-side caching: server-pushed invalidation of an L1"
date: 2026-08-10
track: microservices
summary: "A near-cache in front of Redis is fast until it serves stale data. Redis tracking inverts the coherence problem: the server records which keys a connection has read and pushes an invalidation over RESP3 when they change — no polling, no guessed TTLs. This article covers default tracking, BCAST prefixes, OPTIN/OPTOUT, the RESP2 fallback, and the read-versus-invalidate race."
reading_time: 7
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

**Gist.** An in-process level-1 (L1) near-cache in front of Redis removes a network round-trip per read but has no way of learning that its copy is stale. Redis **tracking** makes the server record which keys a connection has read and push an **invalidation message** when one of those keys changes, so freshness is bounded by the latency of one push rather than by a guessed time-to-live (TTL). The cost is server-side bookkeeping (an invalidation table, or coarse prefix broadcast in its place), a protocol dependency on RESP3 (or a fragile RESP2 redirect), and a client that must handle a reply arriving *after* the invalidation meant to evict it.

Redis itself already serves as a shared level-2 (L2) cache. For hot keys read thousands of times a second, a copy held in the application process removes the round-trip entirely. The alternatives to server-assisted invalidation are all weak: short TTLs force constant re-fetching and still serve stale data inside the window, and polling is chatty and lagged. Tracking, called *server-assisted client-side caching* in the Redis documentation, moves the notification duty to the writer's side of the system.

(This article covers the Redis mechanism. For the general L1/L2 near-cache pattern and its coherence trade-offs, see [multi-level caching and near-caches](/articles/sys-patterns/2026-08-10-multi-level-caching-near-cache).)

## Default (per-key) tracking

Tracking is enabled per connection:

```
CLIENT TRACKING ON
```

Every read-only command on that connection then causes the server to record that this client may be caching the returned keys. The record lives in a global **invalidation table**, a map from key to the set of client identifiers that read it. The table stores client *IDs* rather than references to client structures.

```
Client 1 -> Server: CLIENT TRACKING ON
Client 1 -> Server: GET foo          # server notes: client 1 cached "foo"
Client 2 -> Server: SET foo newval   # foo changed
Server  -> Client 1: invalidate "foo" # pushed, unsolicited
```

When `foo` is modified, evicted, or expires, every client in that entry receives an invalidation and **the entry is then cleared**: a client is notified exactly once, and is tracked again only after it re-reads the key. The table has a configurable maximum size; when that limit is reached Redis evicts entries by **sending invalidations for keys that did not change**. The invariant this preserves is one-sided — a spurious invalidation costs an unnecessary L1 miss, whereas a missed invalidation would cost a stale read, so the eviction path errs toward the former.

Over **RESP3** the invalidation arrives as a `push` message on the same connection that carries the data, so one multiplexed connection both answers reads and receives invalidations. That property is what makes tracking usable without a second socket.

## BCAST mode with prefixes

Per-key tracking consumes server memory proportional to the number of distinct keys clients cache. **Broadcasting mode** replaces that with a coarser signal:

```
CLIENT TRACKING ON BCAST PREFIX object: PREFIX user:
```

In BCAST mode the server stores **nothing per key**. It keeps a **prefixes table** mapping each registered prefix to the clients subscribed to it, and any write to a key beginning with `object:` or `user:` fans an invalidation out to every client on that prefix — **whether or not that client ever read the key**. The client caches optimistically and absorbs spurious invalidations; the server performs no per-key bookkeeping.

Constraints from the command reference:

- Registered prefixes **cannot overlap**: `foo` and `foob` cannot both be registered.
- Omitting `PREFIX` registers the empty prefix, which yields invalidations for **every key in the keyspace**.
- The full grammar is:

```
CLIENT TRACKING <ON | OFF> [REDIRECT client-id]
  [PREFIX prefix [PREFIX prefix ...]] [BCAST] [OPTIN] [OPTOUT] [NOLOOP]
```

BCAST suits keyspaces confined to a small set of namespaces, where client-side re-fetching is cheaper than server memory. Default tracking suits sparse, precise caching.

## OPTIN, OPTOUT and NOLOOP

Outside BCAST mode every read is tracked by default. Two modes narrow that.

**OPTIN** tracks nothing unless the client asks, per command:

```
CLIENT TRACKING ON OPTIN
CLIENT CACHING YES
GET config:featureflags   # tracked
GET session:tmp:abc       # not tracked
```

`CLIENT CACHING YES` applies to **the single command that immediately follows**, or, when that command is `MULTI` or a Lua script, to the whole transaction or script.

**OPTOUT** tracks everything except what is excluded:

```
CLIENT TRACKING ON OPTOUT
CLIENT CACHING NO
GET counter:live          # not tracked
```

**NOLOOP** suppresses invalidations for keys the connection modified itself. In default mode a tracked key modified by that connection is still **removed from the invalidation table** even though the message to the modifier is suppressed.

## The RESP2 fallback

RESP2 has no push type, so invalidations cannot arrive inline. The documented workaround **redirects** them to a second connection subscribed to a reserved Pub/Sub channel.

```
# Connection A (the invalidation sink); assume its CLIENT ID is 4
SUBSCRIBE __redis__:invalidate

# Connection B (the data connection): redirect invalidations to A
CLIENT TRACKING ON REDIRECT 4
GET foo
```

A write to `foo` then delivers an ordinary Pub/Sub `message` on `__redis__:invalidate` to connection A, whose payload is the array of invalidated key names; a **null message signals `FLUSHALL`/`FLUSHDB`**, meaning the whole local cache must be dropped. The channel reuses the Pub/Sub *transport* but is not a broadcast: only the redirected connection receives its own invalidations.

The failure mode that makes the RESP3 path preferable: Pub/Sub delivery is fire-and-forget, so an invalidation issued while the sink is disconnected is lost rather than queued, and if connection A dies, connection B continues serving reads with no invalidation stream behind them. The documented defences are to **flush the entire L1 on loss of the invalidation connection**, to `PING` the invalidation channel periodically and tear down and flush when it stays quiet past a timeout, and to place a **maximum TTL on every L1 entry** independent of the source key's TTL as a backstop against a missed message.

## The read-versus-invalidate race

The ordering below is possible under any tracking configuration, because the invalidation and the reply are independent messages:

```
[data]  client -> server: GET foo
[inval] server -> client: invalidate foo   # a concurrent writer changed it
[data]  server -> client: "bar"            # the now-stale reply lands last
```

Caching the reply on arrival writes the stale value into the L1 **after** the invalidation intended to evict it, and the entry then survives until its local TTL expires. The fix implemented by client libraries is **invalidate-then-cache** with a placeholder: the key is marked as in-progress *before* the read is issued, an invalidation deletes whatever occupies that slot, and the reply is stored only if the placeholder is still present.

```
cache["foo"] = CACHING_IN_PROGRESS   # before issuing GET
GET foo
# invalidation for foo arrives -> DELETE cache["foo"]
# GET reply "bar" arrives -> store only if cache["foo"] is still present
```

The late reply finds no placeholder and declines to cache, costing one miss. Resolving the race in favour of the value is incorrect.

### Implementation sketch (Scala)

The load-bearing part is the placeholder state machine, not the client transport. Entries occupy three states — pending, resolved, absent — and the invalidation handler is the only writer permitted to remove a pending entry.

```scala
enum Entry:
  case Pending
  case Value(v: String, expiresAt: Long)

final class NearCache(maxTtlMillis: Long):
  private val map = java.util.concurrent.ConcurrentHashMap[String, Entry]()

  /** Returns a cached value, or None and reserves the slot for an in-flight read. */
  def lookupOrReserve(key: String): Option[String] =
    map.compute(key, (_, cur) => cur match
      case Entry.Value(_, exp) if exp <= System.currentTimeMillis => Entry.Pending
      case null                                                   => Entry.Pending
      case other                                                  => other
    ) match
      case Entry.Value(v, _) => Some(v)
      case _                 => None

  /** Stores the reply only if the reservation survived; an invalidation removes it. */
  def completeRead(key: String, v: String): Unit =
    map.computeIfPresent(key, (_, cur) => cur match
      case Entry.Pending => Entry.Value(v, System.currentTimeMillis + maxTtlMillis)
      case other         => other   // a newer reservation or value won; leave it
    )

  /** RESP3 push handler; keys = None encodes the FLUSHALL/FLUSHDB null message. */
  def onInvalidate(keys: Option[Seq[String]]): Unit =
    keys match
      case Some(ks) => ks.foreach(k => map.remove(k))
      case None     => map.clear()
```

`completeRead` uses `computeIfPresent`, so a key removed by `onInvalidate` between reservation and reply is not resurrected. The maximum TTL is applied to the local entry independently of the source key's TTL.

## Client library support

**redis-py** provides server-assisted caching when connected with `protocol=3` (RESP3), maintaining a local cache and wiring up invalidations, and falls back to the `__redis__:invalidate` Pub/Sub channel on RESP2 with the reliability caveat above. **Lettuce** exposes `ClientSideCaching` and `CacheFrontend`, backed by tracking, over a caller-supplied near-cache map. Because tracking is a server feature, it also applies to Valkey and to managed Redis deployments where tracking is enabled.

Verification is direct: start a local Redis, connect with `redis-cli -3` (RESP3), issue `CLIENT TRACKING ON` and `GET foo`, then run `SET foo bar` from a second client and observe the push in the first session. `CLIENT TRACKINGINFO` reports the connection's mode before and after switching to `BCAST PREFIX user:`.

## Pitfalls

- **Storing a read reply unconditionally caches a value the server already invalidated.** The invalidation can overtake the reply on the wire; without the placeholder check the stale entry survives until its local TTL expires.
- **Enabling `BCAST` with no `PREFIX` registers the empty prefix**, so every write in the keyspace invalidates every subscribed client and the near-cache hit rate collapses.
- **Registering overlapping prefixes is rejected**: `PREFIX foo` and `PREFIX foob` cannot coexist on a connection.
- **A dead RESP2 redirect connection is silent.** The data connection keeps serving reads with no invalidation stream, so stale entries persist until their local TTL; the documented response is to flush the L1 on loss of the invalidation connection and to time out a quiet channel.
- **Treating a null invalidation payload as "no keys" misses a database flush.** The null message encodes `FLUSHALL`/`FLUSHDB` and requires clearing the whole local cache.
- **Assuming one invalidation per change per key leaves entries uncached.** The entry is cleared after notification, so a client that does not re-read the key is no longer tracked for it.
- **Assuming every invalidation reflects a real write is wrong**: under invalidation-table memory pressure Redis sends invalidations for keys that did not change.
- **`NOLOOP` suppresses the message, not the state change.** In default mode a key the connection modifies is still removed from the invalidation table, so tracking for it ends.
- **Omitting a local maximum TTL removes the last backstop.** Any dropped or unrouted invalidation then leaves an entry stale for the process lifetime.
