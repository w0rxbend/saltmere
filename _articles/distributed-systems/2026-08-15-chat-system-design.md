---
title: "Design a Chat System (WhatsApp/Slack-Style)"
date: 2026-08-15
track: distributed-systems
summary: "Chat is four subsystems wearing one trench coat: a stateful WebSocket edge, a per-conversation ordering authority, a wide-row message store, and an inbox/receipt sync protocol for flaky mobile clients. Here's the message flow end to end, the Discord-style Cassandra/ScyllaDB schema with time buckets, per-device cursors, and the back-of-envelope numbers to say out loud."
reading_time: 6
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

Start the interview by splitting the problem: a **connection layer** (long-lived sockets, presence), a **messaging core** (ordering, fan-out, persistence), and a **sync layer** (cursors, receipts, offline push). Then size it.

## Back-of-envelope

50M DAU × 40 messages/day = **2B messages/day ≈ 23k msg/s average, ~120k msg/s peak** (5× multiplier). At ~1 KB per stored message (text + metadata + indexes) that's ~2 TB/day before replication — a few PB over five years, which is why the reference stores are LSM-based wide-column DBs, not Postgres. Concurrent connections: assume 20% of DAU online → 10M sockets. WhatsApp famously held **2M+ TCP connections per Erlang/FreeBSD box** (the High Scalability writeup: ~500M users on 11,000 cores), so 10M sockets is dozens-to-hundreds of gateway nodes depending on how hot you run them; Slack runs millions of concurrent WebSockets through an Envoy edge.

## Connection layer

Clients hold one **WebSocket** to a gateway (transport trade-offs are in [the WebSocket/SSE article](/articles/sys-patterns/2026-08-13-websocket-sse-long-polling-realtime)); the gateway is the only stateful edge piece. It maintains:

- **Session registry:** `user_id → {gateway, device_id}` in a shared store (Redis, or Slack-style consistent hashing so a conversation's server is computable). Routing a message to a user = look up their gateway, forward.
- **Presence heartbeats:** client pings every ~30 s; the registry entry carries a TTL of ~2 missed heartbeats, so "online" is just "registry key exists." Broadcast presence changes lazily and only to conversations currently on screen — presence fan-out at Slack-scale is *more* traffic than messages, which is why Slack moved clients to subscription-based presence instead of pushing everyone's status to everyone.

Slack's architecture is the clean mental model: stateless gateways at the edge, **Channel Servers** behind them that are sharded by **consistent hash of channel ID** and hold each channel's in-flight state.

## Message flow and ordering

`sender → gateway → chat service (owner shard for conversation_id) → persist → fan-out to recipient gateways → push for offline devices`.

Ordering only needs to be **per conversation**, and that's the trick: route every message through the conversation's owner shard, which assigns a monotonically increasing `message_id`. Use a Snowflake-style time-ordered 64-bit ID ([details here](/articles/distributed-systems/2026-08-11-distributed-unique-ids-snowflake-uuidv7-ulid)) — it sorts chronologically, doubles as a created-at timestamp, and is exactly what Discord clusters on. Global cross-conversation ordering is neither needed nor worth paying for.

**Group fan-out:** for a 20-person group, the owner shard writes the message once to the conversation log and pushes a pointer to each online member's gateway — fan-out-on-write to *connections*, not to per-user message copies. For 100k-member channels (Slack, Discord servers), don't push the body at all: notify "channel has new messages," let clients pull the range — the same [push/pull hybrid as news feeds](/articles/sys-patterns/2026-08-11-fan-out-on-write-vs-read-feeds).

## Storage: wide rows, bucketed

The canonical schema is Discord's (Cassandra, later ScyllaDB):

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

One partition = one conversation's messages for one time window, sorted newest-first — so "load recent history" and "page older" are single-partition sequential reads. The **bucket** exists because unbounded partitions rot: without it, a busy channel's partition grows past the ~100 MB comfort zone and compaction/repair suffer. Even bucketed, **hot partitions** (a huge channel, everyone reading at once) can drag a node's latency down for every partition it hosts — Discord's fixes were moving to ScyllaDB (trillions of messages, 177 Cassandra nodes → 72 ScyllaDB nodes) and putting Rust **data services** in front that do request coalescing: N concurrent readers of the same row cost one DB query.

## Inbox, sync, and receipts

Mobile clients disconnect constantly, so sync must be resumable:

- **Per-device cursor:** each device stores, per conversation, the last `message_id` it has applied; on reconnect it asks "everything after cursor" — cheap, because that's a clustering-key range scan. Multi-device (Slack, Telegram-style) is just multiple cursors over the same server-side log.
- **Receipts are three distinct facts:** *sent* (server persisted it — first tick), *delivered* (recipient device acked — second tick), *read* (conversation opened — blue). Delivered/read flow as tiny upstream events on the same socket and fan out to the sender; for groups, aggregate (store per-member `last_read_id`, render "read by 12").
- **Offline delivery:** if the registry says no device is connected, enqueue to the user's inbox and fire APNs/FCM with a collapse key so 50 messages become one badge update. WhatsApp is pure **store-and-forward** — the server queue exists only until every device acks, then messages are deleted (also what makes E2E encryption tractable). Slack/Discord are **retained-history** systems: the log is the product, and the DB above is sized for it. Say which model you're building; it changes storage by orders of magnitude.

| Decision | Cheap option | Scale option |
|---|---|---|
| Ordering | DB autoincrement per conversation | Snowflake IDs from owner shard |
| Group delivery | Push body to all members | Push notify + client pull (large channels) |
| History | Store-and-forward, delete on ack | Bucketed wide rows, years of retention |
| Presence | Push all changes | Subscriptions + TTL'd registry keys |

**Try next:** build the smallest honest version — one gateway process, Redis session registry, the bucketed schema in ScyllaDB or SQLite — then kill a client mid-conversation and verify the cursor-based catch-up replays exactly the missed range, no dupes, no gaps.
