---
title: "JetStream: Persistence Inside the NATS Server"
date: 2026-08-04
track: microservices
summary: "NATS JetStream turns a single Go binary into a persistent event broker — streams, durable pull consumers, at-least-once acknowledgement, publisher deduplication, and replay. Written against the current NATS Server 2.x line and the modern nats.go jetstream API."
reading_time: 6
tags: [nats, jetstream, event-driven, messaging, iot, go]
sources:
  - title: "NATS Server releases (nats-io/nats-server)"
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

**Gist.** Core NATS delivers at most once: a message published while a subscriber is disconnected is discarded, which rules the plain bus out as an event broker. **JetStream**, the persistence layer built into the NATS server since 2.2 and enabled with the `-js` flag, retains matching messages in an ordered, sequence-numbered stream and tracks per-consumer acknowledgement state, yielding at-least-once delivery and replay. The cost is the state itself: a file or memory store to size and retain, a redelivery window during which duplicates are visible to the application, and consumer cursors that must be administered as first-class server objects.

Everything below describes the **NATS Server 2.x** line and the `jetstream` package of `nats.go`. The operational profile is a single statically linked Go binary — no external coordination service, no JVM — so the same `nats-server` process runs on a laptop, in a Kubernetes StatefulSet, and on a constrained gateway at the edge of a factory network. Persistence is a configuration toggle rather than a second system.

## What JetStream adds on top of core NATS

Core NATS provides subjects and at-most-once delivery. JetStream layers on four mechanisms: **persistence** (messages written to a file or memory store), **streams** (server-side retention of those messages), **consumers** (stateful cursors recording delivery and acknowledgement), and **flow control** tied to acknowledgement. The composite guarantee is at-least-once delivery; effectively-once *processing* requires publisher deduplication plus an idempotent consumer, and is built by the application rather than promised by the server.

## Streams, subjects, and consumers

The three concepts map loosely onto Kafka's vocabulary without being equivalent, which is the usual source of confusion.

A **subject** is the NATS address space — hierarchical, wildcarded tokens such as `orders.eu.created`. Publishers are unaware that persistence exists; they publish to a subject exactly as in core NATS.

A **stream** is a named, server-managed store that *captures* one or more subjects. Declaring `Subjects: ["orders.>"]` causes the stream to persist every matching message, in order, under a **monotonic sequence number**. Retention is configurable by age, by message count, by total bytes, or via the `WorkQueue` and `Interest` policies, which delete a message once it has been consumed. **One stream captures many subjects** — the inversion of Kafka's topic-as-unit model, and the reason routing granularity is a subject filter rather than a partition count.

A **consumer** is a stateful view over a stream. It records which sequence has been acknowledged, so it survives restarts. Consumers are **push** (the server streams messages to the client) or **pull** (the client requests batches). A **durable pull consumer** is the usual choice for microservices: the client controls batch size, so back-pressure is inherent; multiple workers bound to the same durable share the batches; and the cursor is server-side, so it is not lost when a pod cycles.

## Creating a stream and a durable pull consumer

The `nats` command-line interface models this most directly.

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

`--dupe-window 2m` sets the deduplication window described below. `--ack explicit` requires every message to be acknowledged individually. `--max-deliver 5` caps redelivery attempts before the message is no longer redelivered. `--wait 30s` is `AckWait`: the interval after delivery within which an acknowledgement must arrive, and therefore the latency floor for recovering from a crashed worker. Current state is readable with `nats stream info ORDERS` and `nats consumer info ORDERS order-workers`. A round trip through the stream needs two more commands:

```shell
nats pub orders.eu.created '{"id":"A-100"}' -H "Nats-Msg-Id:A-100"
nats consumer next ORDERS order-workers --count 1
```

## Publishing and consuming with acknowledgement (Go)

The current `nats.go` API lives in the `jetstream` package; the older `JetStreamContext` remains functional but is legacy. The shape is context-aware and batch-oriented.

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

A delivered message admits four responses. `Ack()` confirms success and advances the cursor. `Nak()` requests redelivery without waiting out the deadline. `InProgress()` extends the acknowledgement deadline for work that outlives `AckWait`. `Term()` drops a poison message permanently without consuming the remaining `MaxDeliver` attempts. **A worker that crashes before acknowledging leaves the message unacknowledged until `AckWait` expires, after which it is redelivered** — the at-least-once guarantee, and the exact window in which the application must tolerate a duplicate.

## Acknowledgement policies, deduplication, and replay

Three **acknowledgement policies** exist. `explicit` requires an acknowledgement per message and is the policy a work queue depends on. `all` treats an acknowledgement of sequence *N* as acknowledging every earlier sequence, which is cheaper for strictly ordered batch processing but discards the ability to acknowledge out of order. `none` acknowledges nothing, returning delivery semantics to those of core NATS.

**Deduplication** operates on the publish path. With a `dupe-window` configured on the stream and a `Nats-Msg-Id` header attached to each publish, the server rejects a second message carrying an identifier it has already seen within that window, so a publisher retry following an ambiguous network failure does not append a second copy. The window is finite: **a retry that arrives after the window has elapsed is accepted as a new message**. Deduplication therefore removes publisher-side duplicates only; consumer-side duplicates from redelivery remain, and an idempotent handler is required for effectively-once processing end to end.

**Replay** follows from the stream being a retained log. A consumer created with `--deliver all` reprocesses from the oldest message the stream still retains; `--deliver new` begins at the tip; a start point can also be pinned by sequence or by timestamp (`--start-time`). Because the consumer is a separate server object from the stream, a throwaway consumer can reprocess a range of history into a rebuilt read model and then be deleted, leaving the stream and every other consumer's cursor untouched.

## Positioning relative to Kafka

Kafka suits very high sustained throughput, large partition fan-out, and a mature stream-processing ecosystem (Flink, Kafka Streams, Connect). JetStream's distinguishing properties are operational: no external coordination service, subject-filter routing rather than a fixed partition count, and a single binary that clusters with Raft for high availability. For edge and Internet-of-Things (IoT) deployments — many intermittently connected devices, constrained gateways, leaf-node topologies syncing to a central cluster — the small footprint and built-in persistence are the deciding factors.

## Pitfalls

- **A handler that takes longer than `AckWait` is redelivered while it is still running**, producing concurrent duplicate processing; the cause is that the server tracks only the deadline, not liveness, so long work must call `InProgress()` to extend it.
- **A publisher retry arriving after `dupe-window` has elapsed appends a duplicate message**, because the deduplication table only covers that window; a `Nats-Msg-Id` alone guarantees nothing about older retries.
- **Publishing without a `Nats-Msg-Id` header bypasses deduplication entirely**, since the server has no identifier to compare, and the `dupe-window` setting appears configured while having no effect.
- **Under `ack all`, acknowledging a message that was processed out of order silently acknowledges every earlier unprocessed sequence**, permanently advancing the cursor past messages that were never handled.
- **A message exhausting `MaxDeliver` stops being redelivered and is not routed anywhere by default**, so a persistently failing handler loses events unless the advisory for exhausted deliveries is consumed.
- **A `WorkQueue` retention stream deletes a message once it is consumed**, which makes later replay impossible; the retention policy, not the consumer, decides whether history exists.
- **Memory-backed storage discards the entire stream on server restart**, defeating the durability the durable consumer implies; `--storage file` is what makes the log survive a process exit.
