---
title: "Event-based batch processing: single-purpose stages wired by queues"
date: 2026-07-30
track: sys-patterns
summary: "Brendan Burns' event-driven batch pattern — chaining small stages (copier, filter, splitter, sharder, merger) where each stage's output topic is the next stage's input. Why the message wiring makes stages scale and reconfigure independently, plus back-pressure and at-least-once semantics, with a Python filter-and-republish consumer."
reading_time: 5
tags: [event-driven, batch, pipeline, kafka, pubsub, backpressure, at-least-once, burns]
sources:
  - title: "Designing Distributed Systems, 2nd Edition — Ch. 12 Event-Driven Batch Processing (Brendan Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch12.html"
  - title: "Designing Distributed Systems (free 1st-edition ebook, Microsoft)"
    url: "https://info.microsoft.com/rs/157-GQE-382/images/EN-CNTNT-eBook-DesigningDistributedSystems.pdf"
  - title: "Kafka Streams DSL — stateless transformations and writing to output topics (Confluent)"
    url: "https://docs.confluent.io/platform/current/streams/developer-guide/dsl-api.html"
  - title: "RabbitMQ Tutorial 5 — Topics (routing by pattern to multiple queues)"
    url: "https://www.rabbitmq.com/tutorials/tutorial-five-python"
  - title: "Build a one-to-many Pub/Sub system — Google Cloud documentation"
    url: "https://cloud.google.com/pubsub/docs/building-pubsub-messaging-system"
---

A [work queue](/articles/sys-patterns/work-queue-pattern) is one queue drained by interchangeable workers: every item gets the same treatment. But most real batch jobs aren't one step. You ingest raw events, drop the junk, enrich the survivors, partition by customer, and roll up per region. The naive move is to cram all of that into one program that loops over the input and does each step inline — a **monolithic batch job**.

Brendan Burns' **event-driven batch processing** pattern (Chapter 12 of *Designing Distributed Systems*, 2nd ed.) is the alternative: build each step as its own single-purpose stage, and wire the stages together with message queues, where *the output of one stage's queue is the input to the next*. The wiring — not the code inside any stage — is where the leverage is.

## The stages, and why the queue between them matters

The reusable stages are the same vocabulary the [coordinated-batch article](/articles/sys-patterns/coordinated-batch-workflow) enumerated — **copier** (duplicate a stream to N consumers), **filter** (drop non-matching events), **splitter** (fan one event into several), **sharder** (route by key), **merger** (recombine streams). That article was about the *coordinated* case: a barrier and a reduce, where the final answer needs every shard done, mapped onto a DAG runtime.

This is the other case. Event-driven means there is **no orchestrator and no barrier**. Each stage is a long-lived consumer that reads from an input topic, does one transform, and publishes to an output topic. There is no central plan of the pipeline — the topology *is* the set of topic subscriptions. Burns' framing: chaining these queues together "allows for the construction of complicated event-driven workflows out of simple reusable components."

The queue between two stages is doing three jobs at once:

- **Decoupling.** The filter stage doesn't call the enrich stage; it publishes to a topic. Neither knows the other's address, count, or health.
- **Buffering.** If enrich is momentarily slow, events pile up in its input topic instead of blocking the filter.
- **Reconfiguration point.** Want to add a dedup stage between filter and enrich? Point filter at a new topic, subscribe dedup to it, and have dedup publish to enrich's old input. No stage's code changes. That is the payoff over a monolith: the monolith's steps are joined by *function calls*, so reordering or inserting one means editing and redeploying the whole thing.

## Independent scaling — the concrete win

In a monolith, every step runs at the same multiplicity: N copies of the process means N copies of *each* step, whether that step needs it or not. If parsing is cheap and enrichment calls a slow external API, you over-provision parsing to feed enrichment.

With stages wired by queues, each stage is scaled by its own consumer count against its own topic. Enrichment is the bottleneck? Run 20 enrich consumers and 2 filter consumers. Every mainstream bus supports this directly: Kafka via consumer groups on a partitioned topic, RabbitMQ via [multiple consumers on a queue](https://www.rabbitmq.com/tutorials/tutorial-five-python) behind a topic exchange, [Cloud Pub/Sub](https://cloud.google.com/pubsub/docs/building-pubsub-messaging-system) via multiple subscribers on a subscription. The queue depth is your scaling signal — a growing backlog on one topic tells you exactly which stage to add capacity to.

## A filter stage: consume, transform, republish

Here is the whole shape of an event-driven stage — a filter that reads raw events, keeps only paid orders, and republishes them to the next stage's topic. It is deliberately unremarkable; that's the point. (Kafka Streams expresses the same thing declaratively as `stream.filter(...).to("orders.paid")` — see the [DSL stateless operations](https://docs.confluent.io/platform/current/streams/developer-guide/dsl-api.html).)

```python
import json
from kafka import KafkaConsumer, KafkaProducer

IN_TOPIC  = "orders.raw"
OUT_TOPIC = "orders.paid"

consumer = KafkaConsumer(
    IN_TOPIC,
    group_id="filter-paid",          # scale by adding members to this group
    enable_auto_commit=False,        # we commit only after the send is durable
    max_poll_records=100,
    value_deserializer=lambda b: json.loads(b),
)
producer = KafkaProducer(
    value_serializer=lambda v: json.dumps(v).encode(),
    acks="all",                      # wait for replicas before considering it sent
)

def keep(order: dict) -> bool:
    return order.get("status") == "paid"

for batch in iter(lambda: consumer.poll(timeout_ms=1000), None):
    for tp, records in batch.items():
        for rec in records:
            order = rec.value
            if keep(order):
                producer.send(OUT_TOPIC, order)   # this stage's output = next stage's input
    producer.flush()                 # make the republished events durable...
    consumer.commit()                # ...THEN advance our input offset
```

Two ordering rules carry all the correctness:

1. **Flush the output before committing the input.** Send-then-commit means a crash between the two only ever *re-processes* input, never *loses* output. Commit-then-send would drop events on a crash.
2. **`acks="all"` on the producer.** The republished event isn't "done" until the broker has replicated it, or a stage crash could acknowledge input for output that never survived.

## Back-pressure and at-least-once are not optional

Because stages run at different speeds, a fast upstream stage can outrun a slow downstream one. The queue absorbs the difference — until it can't. **Back-pressure** is what stops the buffer from growing without bound: a pull-based consumer (Kafka, Pub/Sub) naturally applies it, because a saturated stage simply polls slower, its lag grows, and the upstream stage's writes eventually block or get throttled on a bounded topic. A push-based fan-out with no bound will instead exhaust memory or start dropping. Design the buffer bound on purpose; don't discover it in production.

The send-then-commit ordering above buys correctness at a price: **at-least-once delivery.** A crash after `flush()` but before `commit()` replays that batch, so the next stage sees some events twice. This is the same tax the work-queue pattern pays, and the same fix applies — **every stage must be idempotent.** Key downstream writes by a stable event ID (upsert, dedup set, or an idempotent producer) so a replayed event is a no-op rather than a double count. Exactly-once across an arbitrary chain of independently-deployed stages is expensive and usually unnecessary; at-least-once plus idempotent stages is the pattern's normal operating point.

## When to reach for it

Use event-driven batch when the job is a *chain of transforms* on a stream: multiple distinct steps, steps that scale at different rates, or a topology you expect to reshape. Use a plain work queue when there's really one step and any worker can do it. Reach for the coordinated variant only when a final result must wait on every shard — that barrier is exactly what the event-driven pattern deliberately omits, and omitting it is what lets each stage run and scale on its own clock.

**Try next:** Stand up a two-stage pipeline locally with a single `docker-compose.yml` — one Kafka (or RabbitMQ) broker, a `filter` service, and an `enrich` service subscribed to `orders.paid`. Give `enrich` a `time.sleep(0.5)` per event, then flood `orders.raw` and watch the consumer lag on `orders.paid` climb. Scale only `enrich` with `docker compose up --scale enrich=5` and watch the lag drain — back-pressure and independent scaling, both visible in one terminal.
