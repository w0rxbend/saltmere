---
title: "Backpressure: the Flow-Control Pattern That Keeps a Fast Producer From Drowning a Slow Consumer"
date: 2026-07-31
track: sys-patterns
summary: "When a producer outruns a consumer, one of three things must happen: the producer blocks, the excess is buffered, or the excess is dropped. Backpressure is the upstream feedback signal that makes the producer match consumer capacity — the same credit model as TCP's receive window and Reactive Streams' request(n) — and unbounded buffering only relocates the failure."
reading_time: 6
tags: [backpressure, flow-control, reactive-streams, tcp, streaming, queues]
sources:
  - title: "Reactive Streams — specification and rationale"
    url: "https://www.reactive-streams.org/"
  - title: "Reactive Streams Specification for the JVM (Subscriber/Subscription rules)"
    url: "https://github.com/reactive-streams/reactive-streams-jvm"
  - title: "RFC 9113: HTTP/2, §5.2 Flow Control (WINDOW_UPDATE)"
    url: "https://www.rfc-editor.org/rfc/rfc9113.html"
  - title: "ReactiveX — Backpressure operators (Buffer / Drop / Latest)"
    url: "https://reactivex.io/documentation/operators/backpressure.html"
  - title: "The Reactive Manifesto — Glossary: Back-Pressure"
    url: "https://www.reactivemanifesto.org/glossary"
---

**Gist.** Whenever one pipeline stage produces faster than the next stage consumes, the queue between them grows without bound, the excess is discarded, or the producer is made to wait. Backpressure is the third outcome arranged deliberately: a feedback signal travelling *upstream* that limits the producer to the consumer's measured capacity. The cost is that the slowdown propagates — the whole pipeline runs at the speed of its slowest stage, and the producer must be a party that can be paused at all.

## The credit model, from TCP to request(n)

Transmission Control Protocol (TCP) flow control is credit-based. The receiver advertises a **receive window** in every acknowledgement — a count of further bytes it is willing to accept — and the sender may hold at most that many unacknowledged bytes in flight. The invariant is one-sided: **bytes in flight ≤ the last advertised window**. When the receiving application stops draining the socket buffer, successive advertisements shrink toward zero, the sender stalls, and no segment is lost. Recovery is explicit rather than timed: the receiver sends a window update once space reappears.

HTTP/2 applies the identical construction one layer higher (RFC 9113 §5.2). Receivers grant octets of credit with `WINDOW_UPDATE` frames, maintained **both per stream and per connection**, with a default initial window of **65,535 bytes**. Only `DATA` frames are subject to flow control, so control frames cannot be blocked behind an exhausted window. The property that surprises operators of gRPC and other HTTP/2 transports is that these windows are **per hop, not end-to-end**: every intermediary in the path maintains its own windows, so a stalled origin does not automatically stall the client — it stalls the nearest proxy first, which may itself hold a full window of buffered data.

**Reactive Streams** raises the same credit idea from octets to application objects. The consumer sets the pace: no element is delivered until the `Subscriber` calls `Subscription.request(n)`, granting demand for `n` elements. Three rules carry the weight. Demand is **additive** across calls — a request of 8 followed by another 8 permits up to 16 elements. **`request(0)` or a negative value is a specification violation**, signalled to the subscriber as an error rather than silently ignored. **`Long.MAX_VALUE` denotes effectively unbounded demand**, which turns backpressure off for that subscription. The demand signal itself is required to be non-blocking and to return promptly, which is what allows the chain to remain both non-blocking and bounded at the same time; `request(n)` is a window advertisement in higher-level clothing.

The resulting state machine per subscription is small: outstanding demand *d* starts at zero, `request(n)` sets *d := d + n*, each `onNext` sets *d := d − 1*, and **the publisher may not emit while *d* = 0**. Terminal signals (`onError`, `onComplete`) are exempt from demand — they are delivered regardless of outstanding credit, which is why a cancelled or failed stream cannot deadlock waiting for a request that will never come.

### Implementation sketch (Scala)

A subscriber that grants credit in fixed batches, using `java.util.concurrent.Flow` from the standard library. The load-bearing detail is that the next `request` is issued only after the previous batch has been fully processed, so the batch size is the pipeline's queue bound.

```scala
import java.util.concurrent.Flow.{Subscriber, Subscription}
import scala.compiletime.uninitialized

final class BatchedSubscriber[T](batch: Int, process: T => Unit)
    extends Subscriber[T]:

  private var subscription: Subscription = uninitialized
  private var outstanding: Int = 0        // mirrors publisher-side demand d

  def onSubscribe(s: Subscription): Unit =
    subscription = s
    outstanding = batch
    s.request(batch)                      // nothing is delivered before this call

  def onNext(item: T): Unit =
    process(item)                         // slow work here bounds the whole chain
    outstanding -= 1
    if outstanding == 0 then              // refill only once the batch is drained
      outstanding = batch
      subscription.request(batch)

  def onError(t: Throwable): Unit = ()    // terminal signals ignore demand
  def onComplete(): Unit = ()
```

Refilling eagerly — calling `request(1)` at the top of every `onNext` — is also legal and keeps the pipe fuller, at the cost of allowing one further element to be in flight while the current one is still being processed.

## Block, buffer, or drop

Once demand is exhausted and the producer still has data, exactly three moves exist. The pattern consists of choosing among them explicitly.

**Block** the producer on a bounded queue (`ArrayBlockingQueue.put`). This is the most faithful form of backpressure, because the slowdown propagates the entire way up the chain. It requires a producer that can be paused, and a blocked thread occupies a resource for the duration. `request(n)` and TCP's zero window obtain the same effect without parking a thread.

**Buffer** the overflow (`onBackpressureBuffer`). This absorbs bursts whose duration is shorter than the buffer's capacity, provided the buffer is **bounded and carries an overflow policy**. An *unbounded* buffer is the characteristic trap: it does not remove overload, it relocates it. With bounded service throughput and a sustained arrival rate above it, queue depth grows without limit by conservation alone, and Little's Law (L = λW) relates that growing depth to a proportionally growing residence time, until memory is exhausted. Unbounded buffering converts an immediate, legible symptom into a delayed `OutOfMemoryError` far from its cause.

**Drop** the excess (`onBackpressureDrop` discards items arriving with no pending demand; `onBackpressureLatest` retains only the most recent). Memory stays bounded and latency stays low, which is correct for **replaceable data** — sensor samples, cursor positions, price ticks where only the newest value carries meaning. It is incorrect for anything requiring at-least-once delivery.

The same taxonomy separates three terms that are frequently conflated. **Rate limiting** is a static, preconfigured cap that carries no information about downstream health. **Load shedding** discards work at the edge when there is no upstream party to signal. **Backpressure** is the closed feedback loop that slows the producer to the consumer's actual rate. They compose: backpressure between internal stages, shedding at an ingress whose upstream cannot be signalled.

## Where the controls already exist

Project Reactor and RxJava expose these strategies as operators and propagate `request(n)` through the chain. Akka and Pekko Streams stages are demand-driven Reactive Streams implementations underneath, with an explicit `OverflowStrategy` (`backpressure`, `dropHead`, `dropTail`, `fail`) on every buffer. A Kafka consumer implements the pattern manually: `max.poll.records` bounds the batch returned by each poll, and when processing lags, `consumer.pause(partitions)` stops the fetcher until `resume(...)` is called — the broker's durable offset log serves as the bounded buffer.

## Pitfalls

- **`Long.MAX_VALUE` demand silently disables flow control.** A stream that appears backpressured allocates until the heap is exhausted, because an intermediate operator requested unbounded demand on its behalf.
- **An unbounded queue turns overload into a delayed crash.** The symptom is a heap dump full of queued elements and a stack trace in an unrelated component; the cause is that the arrival rate exceeded service rate for longer than memory allowed.
- **HTTP/2 windows are per hop.** A client observing healthy flow while the origin is stalled is seeing the nearest proxy's window absorb data that the origin never accepted.
- **Blocking a producer thread does not always propagate.** If the producer is an event loop or a shared thread pool, `put()` on a full queue stalls unrelated work sharing that thread, converting flow control into a pipeline-wide stall.
- **Dropping strategies violate delivery guarantees quietly.** `onBackpressureDrop` on a stream whose elements are financial transactions loses records with no error signal, because dropping is a normal outcome for that operator.
- **Refilling demand inside a batch defeats the bound.** Calling `request(batch)` at the start of each `onNext` instead of after the batch drains lets the publisher keep a full extra batch in flight, doubling the intended queue bound.
