---
title: "Backpressure: the Flow-Control Pattern That Keeps a Fast Producer From Drowning a Slow Consumer"
date: 2026-07-31
track: sys-patterns
summary: "When a producer outruns a consumer, something has to give: block, buffer, or drop. Backpressure is the feedback signal that makes the producer slow down — the same credit-based idea as TCP's receive window and Reactive Streams' request(n) — and why unbounded buffering only hides the failure."
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

Any time one stage produces faster than the next can consume, you are one design decision away from an outage. The queue between them either fills unboundedly until the process dies of an `OutOfMemoryError`, or you start dropping data, or you make the producer wait. **Backpressure** is the third option done deliberately: a feedback signal that flows *upstream* and tells the producer to match the consumer's real capacity. It's a pattern you'll recognize once you see it, because the network stack under your feet has been doing it the whole time.

## The credit model, from TCP to request(n)

TCP flow control is credit-based. The receiver advertises a **receive window** in every ACK — "I can accept this many more bytes" — and the sender may have at most that many unacknowledged bytes in flight. When the application stops draining, the advertised window shrinks toward zero, the sender stalls, and nothing is lost. HTTP/2 does the identical thing one layer up (RFC 9113 §5.2): receivers grant octets with `WINDOW_UPDATE` frames, per-stream *and* per-connection, default window 65,535 bytes. Only `DATA` frames are flow-controlled so control frames can never be blocked. The catch that bites gRPC users: these windows are **per hop**, not end-to-end — every proxy in the path applies its own.

**Reactive Streams** lifts the same credit idea to application objects. The consumer drives the pace: nothing is delivered until the `Subscriber` calls `Subscription.request(n)` to grant demand for `n` elements. Demand is **additive** across calls (request 8, then request 8 more, and up to 16 may flow), `request(0)` or a negative is a spec violation, and `Long.MAX_VALUE` means "fire hose — backpressure off". Crucially, the signaling itself must be non-blocking and return promptly, which is what keeps the whole chain non-blocking while still bounded. `request(n)` is just advertising a window in higher-level clothing.

```java
class BoundedSubscriber<T> implements Subscriber<T> {
    private static final int BATCH = 16;
    private Subscription subscription;
    private int pending;

    public void onSubscribe(Subscription s) {
        this.subscription = s;
        this.pending = BATCH;
        s.request(BATCH);              // grant initial credit; nothing arrives until we ask
    }
    public void onNext(T item) {
        process(item);                 // slow work here bounds the entire pipeline
        if (--pending == 0) {          // only ask for more once the batch is drained
            pending = BATCH;
            subscription.request(BATCH);
        }
    }
    public void onError(Throwable t) { t.printStackTrace(); }
    public void onComplete() { }
    private void process(T item) { /* ... */ }
}
```

## Block, buffer, or drop

When the producer runs ahead and demand is exhausted, you have exactly three moves, and the pattern is choosing consciously:

**Block** the producer on a bounded queue (`ArrayBlockingQueue.put`). This is the truest backpressure — the slowdown propagates all the way up — but it requires a producer you can actually pause, and a blocked thread ties up a resource. `request(n)` and TCP's zero window achieve the same effect without literally parking a thread.

**Buffer** the overflow (`onBackpressureBuffer`). Fine for absorbing short bursts, provided the buffer is **bounded** and has an overflow policy. An *unbounded* buffer is the trap: it doesn't solve overload, it relocates it. Little's Law is blunt here — bounded throughput plus unbounded arrival means unbounded queue depth, so latency climbs without limit until memory runs out. Unbounded buffering converts a fast producer into a delayed, harder-to-diagnose crash.

**Drop** the excess (`onBackpressureDrop` discards items with no pending demand; `onBackpressureLatest` keeps only the newest). Bounded memory, low latency, and correct for replaceable data — sensor readings, cursor positions, stock ticks where only the latest matters. Wrong for anything that needs at-least-once delivery.

This is also the clean line between three often-confused terms. **Rate limiting** is a static, preset cap that ignores real downstream health. **Load shedding** *drops* work at the edge when you can't push back. **Backpressure** is the feedback loop that makes the producer slow to the consumer's actual speed. They compose: backpressure internally, shed at an ingress that has no upstream to signal.

## Where you already have the knobs

Project Reactor and RxJava expose the strategies as operators and propagate `request(n)` for you. Akka/Pekko Streams stages are demand-driven Reactive Streams underneath, with an explicit `OverflowStrategy` (`backpressure`, `dropHead`, `dropTail`, `fail`) on every buffer. A Kafka consumer does it by hand: bound each poll with `max.poll.records`, and when processing lags, call `consumer.pause(partitions)` / `resume(...)` so the fetcher stops — the durable offset log on the broker *is* your bounded buffer.

**Try next:** Wire a producer emitting 10,000 items/s to a consumer that sleeps 10 ms per item, first through an unbounded `LinkedBlockingQueue` and watch heap and latency climb until it dies. Then swap in a bounded `ArrayBlockingQueue(1000)` with `put()` and confirm the producer blocks and memory flattens — you've just converted a crash into a throttle.
