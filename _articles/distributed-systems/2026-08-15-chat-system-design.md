---
title: "Designing a Chat System (WhatsApp/Slack-Style)"
date: 2026-08-15
track: distributed-systems
summary: "A chat system decomposes into four subsystems: a stateful WebSocket edge, a per-conversation ordering authority, a wide-row message store, and a cursor-based sync protocol for intermittently connected devices. This article traces the message path end to end, the Discord-style Cassandra/ScyllaDB schema with time buckets, per-device cursors, and the sizing arithmetic behind them."
reading_time: 7
tags: [chat, websocket, cassandra, scylladb, message-ordering, system-design]
sources:
  - title: "Discord Engineering — How Discord Stores Trillions of Messages"
    url: "https://discord.com/blog/how-discord-stores-trillions-of-messages"
  - title: "Slack Engineering — Real-time Messaging"
    url: "https://slack.engineering/real-time-messaging/"
  - title: "High Scalability — How WhatsApp Grew to Nearly 500 Million Users, 11,000 Cores, and 70 Million Messages a Second"
    url: "https://highscalability.com/how-whatsapp-grew-to-nearly-500-million-users-11000-cores-an/"
  - title: "Slack Engineering — Migrating Millions of Concurrent Websockets to Envoy"
    url: "https://slack.engineering/migrating-millions-of-concurrent-websockets-to-envoy/"
---

**Gist.** A chat system must deliver messages within human reaction time to devices that disconnect constantly, while preserving a single agreed order inside each conversation and retaining (or deliberately discarding) history at petabyte scale. The mechanism is a stateful WebSocket edge whose sessions are indexed in a shared registry, a per-conversation owner shard that assigns monotonically increasing identifiers, and a bucketed wide-column log that clients resume from by cursor. The cost is that ordering authority becomes a per-conversation single point of serialisation, and the edge — unlike the rest of the stack — cannot be made stateless.

The problem separates into a **connection layer** (long-lived sockets, presence), a **messaging core** (ordering, fan-out, persistence), and a **sync layer** (cursors, receipts, offline push).

## Sizing

Fifty million daily active users (DAU) at 40 messages per day yield **2 × 10⁹ messages/day ≈ 23,000 messages/s average**, and **≈120,000 messages/s peak** under a 5× peak-to-mean multiplier. At roughly 1 KB stored per message (body, metadata, index entries) that is about **2 TB/day before replication**, and a few petabytes over five years. That volume is the reason the reference implementations use log-structured merge-tree (LSM) wide-column stores rather than a row-store relational database: the workload is append-dominated with range reads over a recent suffix.

Concurrent connections follow from the online fraction: 20% of DAU online gives **10 million simultaneous sockets**. WhatsApp reported serving nearly 500 million users on 11,000 cores, with **millions of concurrent connections per Erlang/FreeBSD machine**. Ten million sockets therefore falls in the range of dozens to hundreds of gateway nodes, depending on how heavily each is loaded; no published figure fixes the number for a given hardware profile. Slack routes millions of concurrent WebSockets through an Envoy edge.

## Connection layer

Each client holds one **WebSocket** to a gateway; transport alternatives are compared in [the WebSocket/SSE article](/articles/microservices/2026-08-10-realtime-websocket-sse-longpoll). The gateway is the only stateful component at the edge, and it maintains two structures.

The **session registry** maps `user_id → {gateway, device_id}` in a shared store such as Redis. The alternative is to compute the owning server from the identifier by consistent hashing, which removes the lookup at the cost of a rebalance whenever the ring changes. Delivering a message to a user reduces to resolving the gateway and forwarding.

**Presence** is derived from heartbeats: the client pings on a fixed interval (on the order of 30 s) and the registry entry carries a time-to-live (TTL) of about two missed heartbeats, so "online" is the predicate *registry key exists*. The invariant that matters is that presence is **soft state** — it is reconstructed from live connections and never repaired from durable storage. Presence fan-out is a major traffic source at Slack's scale — a status change is of interest to every member of every shared channel — which is why clients subscribe to the presence of a bounded set of users rather than receiving every status change.

Slack separates the two concerns: an edge that terminates the sockets — since migrated behind Envoy — and **Channel Servers** behind it, sharded by **consistent hash of channel identifier**, holding each channel's in-flight state. The edge then holds only connections, and the per-channel routing state lives one hop back.

## Message path and ordering

The path is `sender → gateway → chat service (owner shard for conversation_id) → persist → fan-out to recipient gateways → push notification for offline devices`.

Ordering is required only **per conversation**. Routing every message for a conversation through that conversation's owner shard makes the shard the sole assigner of a monotonically increasing `message_id`, which establishes a total order within the conversation without any cross-shard coordination. A Snowflake-style time-ordered 64-bit identifier ([construction here](/articles/distributed-systems/2026-08-11-distributed-unique-ids-snowflake-uuidv7-ulid)) sorts chronologically and encodes its own creation time; Discord clusters messages on exactly such an identifier. Global ordering across conversations is not required and is not purchased.

**Group fan-out** writes the message once to the conversation log and pushes a pointer to each online member's gateway: fan-out-on-write to *connections*, not to per-user copies of the body. For channels with 100,000 members the body is not pushed at all — the server signals that the channel has new messages and clients pull the range, the [push/pull hybrid used for feeds](/articles/sys-patterns/2026-08-11-fan-out-on-write-vs-read-feeds).

## Storage: bucketed wide rows

The reference schema is Discord's, on Cassandra and later ScyllaDB:

```sql
CREATE TABLE messages (
  channel_id bigint,
  bucket     int,            -- static time window (~10 days)
  message_id bigint,         -- Snowflake: time-sortable
  author_id  bigint,
  content    text,
  PRIMARY KEY ((channel_id, bucket), message_id)
) WITH CLUSTERING ORDER BY (message_id DESC);
```

One partition holds one conversation's messages for one time window, sorted newest-first, so "load recent history" and "page backwards" are single-partition sequential reads. The **bucket** bounds partition growth: without it a busy channel's partition grows past the ~100 MB range that compaction and repair handle comfortably. Even bucketed, a **hot partition** — a large channel read concurrently by many clients — degrades latency for every partition co-resident on that node. Discord's reported remedies were a migration to ScyllaDB (**177 Cassandra nodes replaced by 72 ScyllaDB nodes**) and Rust **data services** in front of the store performing request coalescing, so that N concurrent readers of the same row issue one database query.

## Inbox, sync, and receipts

Because mobile clients disconnect frequently, synchronisation must be resumable rather than incremental-push-only.

- **Per-device cursor.** Each device records, per conversation, the last `message_id` it has applied. On reconnect it requests everything after that cursor, which is a clustering-key range scan inside a single partition. Multi-device accounts are several cursors over one server-side log.
- **Receipts are three distinct facts.** *Sent* means the server persisted the message; *delivered* means a recipient device acknowledged it; *read* means the conversation was opened. Delivered and read travel upstream on the same socket and fan out to the sender. For groups the aggregate is stored per member as `last_read_id` and rendered as a count.
- **Offline delivery.** When the registry shows no connected device, the message is enqueued to the user's inbox and a push notification is emitted through APNs or FCM with a collapse key, so a burst of messages coalesces into one badge update.

The retention model is the decision with the largest cost consequence. WhatsApp is **store-and-forward**: the server queue exists only until every device acknowledges, after which the message is deleted — a design compatible with end-to-end encryption, since the server never needs to read the body. Slack and Discord are **retained-history** systems in which the log is the product and the store above is sized accordingly. The two models differ in storage requirement by orders of magnitude.

### Implementation sketch (Scala)

The load-bearing element is the owner shard: a single-threaded assigner per conversation that stamps an identifier, appends, then fans out. Serialising by conversation is what makes the order total.

```scala
final case class Message(id: Long, convId: Long, author: Long, body: String)

trait Store:
  def append(m: Message): Unit
  def since(convId: Long, bucket: Int, cursor: Long): Seq[Message]

trait Gateway:
  def deliver(m: Message): Unit

trait Registry:
  def sessions(user: Long): Seq[Gateway]

trait Push:
  def enqueue(user: Long, messageId: Long): Unit

final class OwnerShard(store: Store, registry: Registry, push: Push, nextId: () => Long):
  // One lock per conversation: concurrent conversations proceed in parallel,
  // but a single conversation has exactly one assigner of message ids.
  private val locks = scala.collection.concurrent.TrieMap.empty[Long, AnyRef]

  def submit(convId: Long, author: Long, body: String, members: Seq[Long]): Long =
    val lock = locks.getOrElseUpdate(convId, new AnyRef)
    lock.synchronized:
      val m = Message(nextId(), convId, author, body)
      store.append(m)                       // durable before any delivery
      members.foreach: u =>
        val gws = registry.sessions(u)
        if gws.isEmpty then push.enqueue(u, m.id)  // offline: inbox + collapse-key push
        else gws.foreach(_.deliver(m))
      m.id

  def resume(convId: Long, bucket: Int, cursor: Long): Seq[Message] =
    store.since(convId, bucket, cursor)     // clustering-key range scan
```

`submit` persists before delivering, so a gateway crash costs a retransmission rather than a lost message; `resume` is the reconnect path that repairs it.

| Decision | Low-cost option | Scale option |
|---|---|---|
| Ordering | Per-conversation autoincrement in the database | Snowflake identifiers from the owner shard |
| Group delivery | Push body to all members | Push notification plus client pull for large channels |
| History | Store-and-forward, delete on acknowledgement | Bucketed wide rows, multi-year retention |
| Presence | Push every change | Subscriptions plus TTL'd registry keys |

## Pitfalls

- **Delivering before persisting.** The sender sees a sent receipt, the owner shard crashes before the append commits, and the message is absent from every reader's cursor replay — a permanent gap rather than a retry.
- **Unbucketed partitions.** A single busy channel accumulates one unbounded partition; compaction and repair times grow with it, and the node hosting that partition slows for every other partition it holds.
- **Assigning identifiers at the gateway rather than the owner shard.** Two gateways stamp concurrent messages from clock-skewed hosts, and clients sorting by identifier render the reply before the message it answers.
- **Treating presence as durable.** Registry entries written without a TTL survive an ungraceful socket close, so users remain "online" indefinitely and messages route to a gateway that no longer holds the connection.
- **Push notifications without a collapse key.** A burst of 50 group messages produces 50 device notifications, and the notification cost, not the message cost, becomes the scaling limit.
- **Cursor advanced on receipt rather than on application.** A client that acknowledges a range before writing it to local storage loses that range on a crash, and the next reconnect requests only newer messages, leaving a hole no retry closes.
- **Ignoring the retention model at design time.** A store-and-forward queue sized for transient state cannot absorb a later product decision to retain history; the storage requirement changes by orders of magnitude, not by a factor.
