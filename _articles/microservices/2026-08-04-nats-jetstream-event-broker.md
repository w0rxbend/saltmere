---
title: "JetStream: Persistence Without the Kafka Tax"
date: 2026-08-04
track: microservices
summary: "NATS JetStream turns a single 20MB Go binary into a persistent event broker — streams, durable pull consumers, at-least-once acks, message dedup, and replay. Verified against NATS Server 2.14.2 and the modern nats.go jetstream API."
reading_time: 5
tags: [nats, jetstream, event-driven, messaging, iot, go]
sources:
  - title: "NATS Server 2.14.2 release (nats-io/nats-server)"
    url: "https://github.com/nats-io/nats-server/releases"
  - title: "JetStream Walkthrough | NATS Docs"
    url: "https://docs.nats.io/nats-concepts/jetstream/js_walkthrough"
  - title: "JetStream Model: Streams and Consumers | NATS Docs"
    url: "https://docs.nats.io/nats-concepts/jetstream"
  - title: "NATS by Example — Pull Consumers (Go)"
    url: "https://natsbyexample.com/examples/jetstream/pull-consumer/go/"
  - title: "NATS JetStream in Practice: Persistent Messaging and Event Replay"
    url: "https://timderzhavets.com/blog/nats-jetstream-in-practice-persistent-messaging-and/"
---

Core NATS is a fire-and-forget message bus: if a subscriber isn't connected when a message is published, that message is gone. That's fine for RPC and ephemeral fan-out, useless for an event broker. **JetStream** — the persistence layer built into the NATS server since 2.2 and enabled with a single `-js` flag — closes the gap. It gives you durable, replayable streams with acknowledgements, without bolting on a second system. Everything below is verified against **NATS Server 2.14.2** (June 2026 release, built with Go 1.26) and the modern `nats.go` `jetstream` package.

I run IoT backends, so the thing that keeps pulling me back to JetStream is the footprint: one statically-linked Go binary, no ZooKeeper, no KRaft controllers, no JVM. The same `nats-server` runs on a developer laptop, a Kubernetes StatefulSet, and a fanless gateway box at the edge of a factory network. Persistence is a config toggle, not an architecture.

## What JetStream adds on top of core NATS

Core NATS gives you subjects and at-most-once delivery. JetStream layers on four things: **persistence** (messages written to a file or memory store), **streams** (server-side retention of those messages), **consumers** (stateful cursors that track delivery and acks), and **flow control** with acknowledgement. The result is at-least-once delivery by default, and effectively-once processing when you combine publisher dedup with idempotent consumers.

## Streams, subjects, and consumers

These three concepts trip people up because they map loosely onto Kafka but aren't the same thing.

A **subject** is the NATS address space — hierarchical, wildcarded tokens like `orders.eu.created`. Publishers never know or care that persistence exists; they publish to a subject exactly as in core NATS.

A **stream** is a named, server-managed store that *captures* one or more subjects. You declare `Subjects: ["orders.>"]` and the stream persists every matching message, in order, with a monotonic sequence number. Retention is configurable: keep messages by age, by count, by total bytes, or with `WorkQueue`/`Interest` policies that delete a message once it's been consumed. One stream, many subjects — that's the key inversion from Kafka's topic-is-the-unit model.

A **consumer** is a stateful view over a stream. It remembers which sequence you've acknowledged, so it survives restarts. Consumers come in two flavours: **push** (server streams messages at you) and **pull** (you ask for batches). For microservices you almost always want a **durable pull consumer** — it's back-pressure-friendly, scales horizontally (run N workers against the same durable and they load-balance), and doesn't lose its place when a pod cycles.

## Creating a stream and a durable pull consumer

The `nats` CLI is the fastest way to model this. Start a server with JetStream on, then:

```shell
# start a JetStream-enabled server
nats-server -js -sd /var/lib/nats

# create a stream capturing all orders.* subjects, file-backed, keep 7 days
nats stream add ORDERS \
  --subjects "orders.>" \
  --storage file \
  --retention limits \
  --max-age 168h \
  --dupe-window 2m \
  --defaults

# create a durable pull consumer with explicit acks
nats consumer add ORDERS order-workers \
  --pull \
  --ack explicit \
  --deliver all \
  --max-deliver 5 \
  --wait 30s \
  --defaults
```

`--dupe-window 2m` is the message-dedup window (more below). `--ack explicit` means every message must be individually acked. `--max-deliver 5` caps redelivery attempts before the message is considered dead. Inspect it all with `nats stream info ORDERS` and `nats consumer info ORDERS order-workers`. Publish a test event and pull it back:

```shell
nats pub orders.eu.created '{"id":"A-100"}' -H "Nats-Msg-Id:A-100"
nats consumer next ORDERS order-workers --count 1
```

## Publishing and consuming with acks (Go)

The current `nats.go` API lives in the `jetstream` package (the older `JetStreamContext` still works but is legacy). Note the context-aware, batch-oriented shape:

```go
import (
    "context"
    "github.com/nats-io/nats.go"
    "github.com/nats-io/nats.go/jetstream"
)

nc, _ := nats.Connect(nats.DefaultURL)
js, _ := jetstream.New(nc)
ctx := context.Background()

// idempotent publish: the Msg-Id dedupes within the stream's dupe window
js.Publish(ctx, "orders.eu.created", []byte(`{"id":"A-100"}`),
    jetstream.WithMsgID("A-100"))

// bind to the existing durable pull consumer
cons, _ := js.Consumer(ctx, "ORDERS", "order-workers")

// continuous consumption; Ack() advances the durable's cursor
cc, _ := cons.Consume(func(msg jetstream.Msg) {
    if err := handle(msg.Data()); err != nil {
        msg.Nak() // redeliver after AckWait or backoff
        return
    }
    msg.Ack()
})
defer cc.Stop()
```

`Ack()` confirms success and moves the cursor. `Nak()` triggers redelivery; `InProgress()` extends the ack deadline for slow work; `Term()` permanently drops a poison message without waiting out `MaxDeliver`. If a worker crashes before acking, the message reappears after `AckWait` — that's the at-least-once guarantee in action.

## Ack policies, dedup, and replay

Three **ack policies** exist: `explicit` (ack each message — the only sane choice for a work queue), `all` (acking sequence N implicitly acks everything before it — cheap for ordered batch processing), and `none` (fire-and-forget, effectively back to core NATS semantics).

**Dedup** is what gets you toward exactly-once. Set a `dupe-window` on the stream and attach a `Nats-Msg-Id` header to each publish. Within that window the server rejects a second message with the same ID, so a publisher retry after an ambiguous network failure doesn't create a duplicate event. Combine that with an idempotent consumer and you have effectively-once end to end — JetStream doesn't promise magic exactly-once, it gives you the two primitives you need to build it.

**Replay** falls out of the stream being a retained log. A new consumer with `--deliver all` reprocesses history from sequence 1; `--deliver new` starts at the tip; and you can pin a start point by sequence or by timestamp (`--start-time`). Because the consumer is a separate object from the stream, you can spin up a throwaway consumer to reprocess a day of events into a rebuilt read model, then delete it — the stream is untouched.

## When you'd reach for it instead of Kafka

Kafka is the right call for very high sustained throughput, large partition fan-out, and a mature stream-processing ecosystem (Flink, Kafka Streams, Connect). JetStream wins on operational simplicity: no external coordination service, subject-level routing instead of rigid partition math, and a single small binary that clusters with Raft when you need HA. For edge and IoT — thousands of intermittently-connected devices, constrained gateways, leaf-node topologies that sync to a central cluster — the small footprint and built-in persistence make it the more natural fit. You get an event broker without operating a distributed log platform.

**Try next:** Add a second durable pull consumer to the same `ORDERS` stream with `--deliver new`, run three worker instances against it, and watch JetStream load-balance batches across them — then kill one mid-batch and confirm the un-acked messages redeliver to the survivors after `AckWait`.
