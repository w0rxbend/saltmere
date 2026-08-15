---
title: "Windows and watermarks: the Dataflow model applied to click-stream aggregation"
date: 2026-08-13
track: sys-patterns
summary: "Event time and processing time drift apart, so every windowed aggregation must decide when a window is complete — that decision is the watermark. The Dataflow model's what/where/when/how questions, the three window shapes, heuristic versus perfect watermarks, and a Flink SQL example showing which late clicks still count."
reading_time: 6
tags: [stream-processing, watermarks, windowing, event-time, dataflow-model]
sources:
  - title: "The Dataflow Model (Akidau et al., VLDB 2015)"
    url: "https://www.vldb.org/pvldb/vol8/p1792-Akidau.pdf"
  - title: "Streaming 101: The world beyond batch (Tyler Akidau, O'Reilly)"
    url: "https://www.oreilly.com/radar/the-world-beyond-batch-streaming-101/"
  - title: "Streaming 102: The world beyond batch (Tyler Akidau, O'Reilly)"
    url: "https://www.oreilly.com/radar/the-world-beyond-batch-streaming-102/"
  - title: "Apache Flink docs — Windowing table-valued functions (TVF)"
    url: "https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/table/sql/queries/window-tvf/"
  - title: "Watermarks in Stream Processing Systems (Begoli et al., VLDB 2021)"
    url: "http://www.vldb.org/pvldb/vol14/p3135-begoli.pdf"
---

**Gist.** On an unbounded stream, an aggregation such as "clicks per user per minute" cannot wait for the input to end, so the system needs a rule for declaring a window complete. That rule is the **watermark**: a monotonically advancing event-time threshold asserting that all events at or below it have arrived, which allows a window to fire and its state to be released. The cost is paid twice — the watermark delay adds latency to every result, and events arriving behind the watermark are **late** and must be dropped, reprocessed, or diverted.

The [Dataflow paper](https://www.vldb.org/pvldb/vol8/p1792-Akidau.pdf) (Akidau et al., VLDB 2015) decomposes such a pipeline into four independent decisions: **what** is computed (the aggregation), **where** in event time it is computed (windowing), **when** results are emitted (watermarks and triggers), and **how** refinements of the same window relate to each other (accumulation). A `GROUP BY` answers only the first.

## Two clocks that never agree

Every event carries an **event time** (when the click occurred, stamped at the source) and a **processing time** (when the pipeline observes it). The two diverge; [Streaming 101](https://www.oreilly.com/radar/the-world-beyond-batch-streaming-101/) names the gap *skew* and notes it is unbounded in practice. A phone that loses connectivity buffers clicks and flushes them on reconnection, so those events arrive both late **and out of order** with respect to event time.

Windowing by processing time places in a bucket whatever happened to arrive during that interval. That is cheap and adequate for *observed* rates. Questions about when events occurred — billing, sessionization, fraud — require event-time windowing, which introduces the completeness problem: how long to wait for stragglers.

## Where: three window shapes

- **Tumbling (fixed)**: contiguous, non-overlapping intervals such as `[12:00, 12:01)` and `[12:01, 12:02)`. Each event falls in exactly one window, so it contributes to exactly one aggregate rather than to several at once.
- **Hopping (sliding)**: fixed size with a smaller advance. Size one minute, hop ten seconds places each event in six windows; storage and compute scale by `size/hop`.
- **Session**: data-driven. A window per key grows while events keep arriving within a *gap timeout* and closes after silence. Session windows have no aligned edges, and an event arriving between two existing sessions merges them. The Dataflow model accordingly splits windowing into two operations rather than one bucketing function: `AssignWindows`, which places an event in windows, and **`MergeWindows`**, which collapses overlapping per-key windows and their state.

## When: what a watermark asserts

A watermark `W(t)` is a value flowing through the pipeline that asserts *all events with event time at or below t have arrived*. When the watermark passes a window's end timestamp, the window is complete as far as the system can tell: it fires, and its state becomes eligible for release.

- A **perfect watermark** makes that assertion a guarantee. The Dataflow paper conditions it on perfect knowledge of the input — the pipeline must be able to establish that no event below `t` remains outstanding. Nothing is ever late, but the pipeline may wait a long time.
- A **heuristic watermark** is an estimate, typically **maximum event time observed minus a bounded delay**. [Streaming 102](https://www.oreilly.com/radar/the-world-beyond-batch-streaming-102/) states the trade-off directly: a watermark that advances too slowly delays results, and one that advances too quickly leaves data arriving *behind* it. Such data is **late**.

The watermark delay is therefore the latency-versus-completeness dial, and late data is a designed-for consequence of choosing a heuristic rather than an anomaly. Begoli et al.'s [VLDB 2021 paper](http://www.vldb.org/pvldb/vol14/p3135-begoli.pdf) is a comparative analysis of how Apache Flink and Google Cloud Dataflow implement the same concept with different propagation mechanics.

The invariant that keeps downstream operators correct is **monotonicity**: a watermark never moves backwards, and an operator with multiple inputs emits the **minimum** of its inputs' watermarks. A single idle input partition therefore holds the whole pipeline's watermark at its last value, and every downstream window stalls.

## Late data, triggers, accumulation

A late event has three sanctioned fates: **drop** it; **reprocess** it by re-firing the window, bounded by an *allowed lateness* horizon after which the state is garbage-collected; or **divert** it to a side output or correction stream. Triggers generalise "fire when the watermark passes the window end" to also firing early — speculative partial results on a processing-time interval — and firing again on late arrivals.

Because a window may fire more than once, each firing needs an **accumulation mode**: *discarding* emits only the delta since the previous firing; *accumulating* emits the updated total, which requires the sink to upsert by window key; *accumulating and retracting* emits a retraction of the previous value alongside the new one, for consumers that re-aggregate the results themselves.

## The example: late clicks in Flink SQL

```sql
CREATE TABLE clicks (
  user_id  STRING,
  url      STRING,
  ts       TIMESTAMP(3),
  -- heuristic watermark: tolerate 10s of out-of-order arrival
  WATERMARK FOR ts AS ts - INTERVAL '10' SECOND
) WITH ('connector' = 'kafka', 'topic' = 'clicks', ...);

-- per-user clicks per minute, tumbling event-time windows
SELECT window_start, window_end, user_id, COUNT(*) AS clicks
FROM TABLE(
  TUMBLE(TABLE clicks, DESCRIPTOR(ts), INTERVAL '1' MINUTE))
GROUP BY window_start, window_end, user_id;
```

The timeline for window `[12:00, 12:01)`:

1. Clicks with `ts` of 12:00:05 and 12:00:58 arrive in order and are buffered into the window.
2. At wall-clock 12:01:04 an event with `ts = 12:00:59` arrives. The watermark stands at roughly `max_ts − 10s ≈ 12:00:49`, **before** the window end, so this out-of-order click is not late and is counted. This is precisely what the ten-second delay purchases.
3. An event with `ts = 12:00:58` arriving after the watermark has passed 12:01:00 **is** late. Flink SQL window table-valued functions (TVFs) drop it, which is visible in the `numLateRecordsDropped` metric. Reprocess-or-divert behaviour requires the DataStream API: `.window(...).allowedLateness(Duration.ofMinutes(5)).sideOutputLateData(tag)` re-fires the window per late event and diverts anything beyond five minutes.

Kafka Streams exposes the same dial as a **grace period** — `TimeWindows.ofSizeAndGrace(ofMinutes(1), ofSeconds(10))` — emitting updates per record, with `suppress()` available to withhold output until the window closes.

### Implementation sketch (Scala)

The load-bearing logic of a heuristic watermark plus tumbling windows fits in a fold over a single keyed partition. State held: per-window counts, and the maximum event time observed.

```scala
final case class Click(user: String, ts: Long)          // ts in epoch millis

final case class WindowState(
    counts: Map[(String, Long), Long],                  // (user, windowStart) -> count
    maxTs: Long
)

val windowMs = 60_000L
val allowedOutOfOrderMs = 10_000L

def windowStart(ts: Long): Long = ts - (ts % windowMs)

/** Returns the updated state, windows fired by this event, and late events. */
def step(s: WindowState, c: Click): (WindowState, Map[(String, Long), Long], List[Click]) =
  val maxTs = math.max(s.maxTs, c.ts)
  val watermark = maxTs - allowedOutOfOrderMs
  val start = windowStart(c.ts)

  // A window is closed once the watermark has reached its exclusive end.
  if start + windowMs <= watermark then (s.copy(maxTs = maxTs), Map.empty, List(c))
  else
    val counts = s.counts.updatedWith((c.user, start))(prev => Some(prev.getOrElse(0L) + 1))
    val (fired, retained) = counts.partition((k, _) => k._2 + windowMs <= watermark)
    (WindowState(retained, maxTs), fired, Nil)
```

The `partition` step is where the completeness decision is made: entries whose window end has been overtaken by the watermark are emitted and evicted, and any later event for those keys takes the late branch. Extending this to allowed lateness means retaining evicted windows for a further horizon and re-emitting on each late arrival, which is the accumulating mode above.

## Pitfalls

- **An idle source partition freezes the watermark.** Because an operator takes the minimum watermark across inputs, one partition receiving no events holds the global watermark at its last value; windows never fire and results stop appearing even though other partitions are busy. Sources that can go idle need an idleness timeout so the partition stops constraining the minimum.
- **Windowing on ingestion time while calling it event time.** If the timestamp is assigned when the pipeline reads the record rather than when the click occurred, out-of-order arrival becomes invisible, no record is ever late, and the counts silently attribute buffered clicks to the wrong minute.
- **Accumulating output into an append-only sink.** Each re-firing emits the full running total for the window, so a sink that appends rather than upserts on `(window, user)` double-counts every refinement.
- **Allowed lateness retains state.** Every window kept open for late arrivals holds its per-key state for the whole horizon, so a five-minute lateness on one-minute windows multiplies retained window state accordingly.
- **Hopping windows multiply cost by `size/hop`.** A one-minute window advancing every second places each event in sixty windows; the aggregation appears smoother while state and throughput cost rise proportionally.
- **Session windows merge retroactively.** An event landing in the gap between two open sessions merges them into one, so any downstream consumer that already received the earlier session's result must handle its revision or retraction.
- **Dropped late records are silent by default.** In Flink SQL the only signal is the `numLateRecordsDropped` metric; a watermark delay shortened below the real arrival skew loses data without producing an error.

Delivery guarantees are a separate concern — see [exactly-once in Kafka](/articles/distributed-systems/2026-08-10-delivery-semantics-exactly-once) — and an aggregator falling behind its ingestion rate is a [backpressure](/articles/sys-patterns/2026-07-31-backpressure-flow-control) problem rather than a windowing one.
