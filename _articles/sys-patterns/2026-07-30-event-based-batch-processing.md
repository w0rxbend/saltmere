---
title: "Event-based batch processing: single-purpose stages wired by queues"
date: 2026-07-30
track: sys-patterns
summary: "Brendan Burns' event-driven batch pattern — chaining small stages (copier, filter, splitter, sharder, merger) where each stage's output topic is the next stage's input. How the message wiring lets stages scale and be reconfigured independently, plus back-pressure and at-least-once semantics, with a filter-and-republish consumer."
reading_time: 6
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

**Gist.** Most batch jobs are a chain of distinct transforms — ingest, discard non-matching records, enrich, partition by key, roll up — and a monolithic job joins those steps with function calls, so every step runs at the same multiplicity and any reordering means editing and redeploying the whole program. Brendan Burns' **event-driven batch processing** pattern (Chapter 12 of *Designing Distributed Systems*, 2nd ed.) instead builds each step as a single-purpose long-lived consumer and wires the steps with message topics, where **the output topic of one stage is the input topic of the next**, so each stage is scaled and rewired independently. The cost is that the pipeline no longer has a single consistent point of failure recovery: each hop is an at-least-once delivery, so every stage must be idempotent, and every inter-stage buffer needs a deliberate bound.

## The stages, and the work the queue between them does

The reusable stages are the vocabulary the [coordinated-batch article](/articles/sys-patterns/2026-07-27-coordinated-batch-workflow) enumerated — **copier** (duplicate a stream to N consumers), **filter** (drop non-matching events), **splitter** (fan one event into several), **sharder** (route by key), **merger** (recombine streams). That article covered the *coordinated* case: a barrier and a reduce, where the final answer requires every shard to have finished, mapped onto a directed acyclic graph (DAG) runtime.

The event-driven case is the other one. It has **no orchestrator and no barrier**. Each stage reads from an input topic, applies one transform, and publishes to an output topic. No component holds a plan of the pipeline — the topology *is* the set of topic subscriptions. Burns' framing is that chaining these queues together composes complicated event-driven workflows out of simple reusable components.

The queue between two stages carries three distinct responsibilities:

- **Decoupling.** The filter stage does not call the enrich stage; it publishes to a topic. Neither stage holds the other's address, instance count, or health state.
- **Buffering.** When enrich is momentarily slow, events accumulate in its input topic rather than blocking the filter.
- **Reconfiguration point.** Inserting a deduplication stage between filter and enrich requires pointing filter at a new topic, subscribing dedup to it, and having dedup publish to enrich's existing input topic. No stage's code changes. In a monolith the steps are joined by function calls, so the same insertion is a source edit and a redeploy of the entire job.

## Independent scaling

In a monolith every step runs at the same multiplicity: N copies of the process means N copies of *each* step, whether or not that step is the constraint. Where parsing is cheap and enrichment calls a slow external API, parsing is over-provisioned to keep enrichment fed.

With stages wired by queues, each stage is scaled by its own consumer count against its own topic: 20 enrich consumers alongside 2 filter consumers is an ordinary configuration. Mainstream buses support this directly — Kafka through consumer groups over a partitioned topic, RabbitMQ through [multiple consumers on a queue](https://www.rabbitmq.com/tutorials/tutorial-five-python) behind a topic exchange, [Cloud Pub/Sub](https://cloud.google.com/pubsub/docs/building-pubsub-messaging-system) through multiple subscribers on a subscription. **Queue depth is the scaling signal**: a backlog growing on one topic identifies the stage that lacks capacity, without any instrumentation inside the stages.

One bound follows from the Kafka mechanism specifically. **Within a consumer group, a partition is assigned to at most one member**, so the useful parallelism of a stage is capped by the partition count of its input topic; adding a twenty-first consumer to a twenty-partition topic leaves that consumer idle.

## The filter stage: consume, transform, republish

A stage is a poll loop with two ordering rules. Kafka Streams expresses the same stateless transform declaratively as `stream.filter(...).to("orders.paid")` — see the [DSL stateless operations](https://docs.confluent.io/platform/current/streams/developer-guide/dsl-api.html).

### Implementation sketch (Scala)

Scala 3 over the Kafka Java client. Imports, serde configuration and error handling are omitted.

```scala
val consumer = KafkaConsumer[String, String](consumerProps)  // enable.auto.commit=false
val producer = KafkaProducer[String, String](producerProps)  // acks=all
consumer.subscribe(java.util.List.of("orders.raw"))

def keep(order: String): Boolean = order.contains("\"status\":\"paid\"")

while true do
  val records = consumer.poll(java.time.Duration.ofSeconds(1))
  records.forEach { rec =>
    if keep(rec.value()) then
      // this stage's output topic is the next stage's input topic; the input
      // key is carried through so downstream partitioning stays stable
      producer.send(ProducerRecord("orders.paid", rec.key(), rec.value()))
  }
  producer.flush()      // republished events durable on the brokers first
  consumer.commitSync() // only then advance this stage's input offset
```

Two ordering rules carry the correctness of the whole hop:

1. **Flush the output before committing the input.** Send-then-commit means a crash between the two re-processes input; it never loses output. Commit-then-send loses events whenever the process dies in the window.
2. **`acks=all` on the producer.** Without it the send is acknowledged before the record is replicated, so a broker failure can lose an output record whose input offset has already been committed — the exact loss the first rule was written to prevent.

## Back-pressure and at-least-once

Stages run at different speeds, so a fast upstream stage can outrun a slow downstream one. The queue absorbs the difference until the queue's bound is reached. **Back-pressure** is what keeps the buffer from growing without bound. A pull-based consumer (Kafka, Pub/Sub) bounds the *in-process* work directly: a saturated stage fetches its next batch only when it is ready for one, so the backlog accumulates in the broker rather than in the consumer's heap. A push-based fan-out with no bound has no such mechanism and instead exhausts memory or drops events.

That is where the honest limit of the pattern sits: moving the backlog to the broker bounds the consumer, not the pipeline. **The buffer bound is a design parameter, and on the usual buses reaching it discards data rather than slowing the producer.** A Kafka topic's retention limit deletes the oldest records once the size or time bound is passed, whether or not a stage has read them; a RabbitMQ queue with a `max-length` limit drops messages at the head by default, and only rejects new publishes when configured with the `reject-publish` overflow behaviour; Pub/Sub drops unacknowledged messages past the subscription's message-retention duration. Genuine upstream throttling has to be built — by rate-limiting the producing stage against observed lag — and is not a property the bus supplies.

The send-then-commit ordering buys durability at a stated price: **at-least-once delivery**. A crash after the flush and before the commit replays the batch, and the next stage observes those events twice. This is the same tax the [work queue](/articles/sys-patterns/2026-07-26-work-queue-pattern) pattern pays, and the same remedy applies — **every stage must be idempotent**. Keying downstream writes by a stable event identifier (an upsert, a deduplication set, or Kafka's idempotent producer) makes a replayed event a no-op rather than a double count. Note that the duplicates compound along the chain: a replay at stage 1 produces duplicate input for stages 2 and 3 as well, so idempotence is required at every stage, not only at the sink.

## When the pattern applies

Event-driven batch fits a *chain of transforms* over a stream: several distinct steps, steps whose throughput requirements differ, or a topology expected to be reshaped. A plain work queue fits when there is one step and any worker can perform it. The coordinated variant is required only when a final result must wait on every shard — that barrier is precisely what the event-driven pattern omits, and its omission is what allows each stage to run and scale on its own clock.

A two-stage pipeline demonstrates both properties locally: one broker, a `filter` service, and an `enrich` service subscribed to `orders.paid` with an artificial half-second delay per event. Flooding `orders.raw` makes consumer lag on `orders.paid` climb; scaling only `enrich` drains it, with the filter stage untouched.

## Pitfalls

- **Committing the input offset before flushing the output** loses every event in the in-flight batch when the process dies in that window, and the loss is silent — the input offset has already advanced past the records.
- **`acks=1` or `acks=0` on an inter-stage producer** reintroduces that same loss at the broker layer even with correct commit ordering, because the input offset advances on a record that was never replicated.
- **A non-idempotent sink** turns an ordinary consumer-group rebalance into double counting: the rebalance replays uncommitted records, and the sink applies them a second time.
- **Adding consumers beyond the input topic's partition count** produces idle members rather than throughput, because a partition is assigned to at most one member of a group.
- **An unbounded push-based fan-out between stages** removes back-pressure entirely; the slow stage's backlog is held in memory and the failure surfaces as an out-of-memory kill rather than as growing lag.
- **Assuming the broker's size or time bound throttles the upstream stage** inverts what the bound does: Kafka retention and a default RabbitMQ `max-length` limit discard messages when the bound is reached, so a backlog that outlives the retention window is lost silently rather than pushing back.
- **Treating queue depth as a health metric rather than a capacity signal** hides which stage is the constraint: depth grows on the topic *upstream* of the saturated stage, not inside it.
- **Rewiring a topology by repointing a producer while consumers are mid-batch** leaves the old topic with records no stage will read; the events are not lost, but nothing consumes them until a subscription is restored.
