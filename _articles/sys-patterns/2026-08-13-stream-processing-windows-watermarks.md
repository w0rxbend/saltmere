---
title: "Windows and watermarks: the Dataflow model answers for \"aggregate a click stream\""
date: 2026-08-13
track: sys-patterns
summary: "Event time and processing time drift apart, so every windowed aggregation must decide when a window is \"done\" — that decision is the watermark. The Dataflow model's what/where/when/how questions, the three window shapes, heuristic vs perfect watermarks, and a Flink SQL example that shows exactly which late clicks still count."
reading_time: 5
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

"Count clicks per user per minute" sounds like a GROUP BY. On an unbounded stream it is four separate decisions, and the [Dataflow paper](https://www.vldb.org/pvldb/vol8/p1792-Akidau.pdf) (Akidau et al., VLDB 2015) names them: **what** you compute (the aggregation), **where** in event time you compute it (windowing), **when** you emit results (watermarks + triggers), and **how** refinements relate (accumulation). Interviewers asking "aggregate a click stream" are really asking whether you know decisions two through four exist.

## Two clocks that never agree

Every event has an **event time** (when the click happened, stamped at the source) and a **processing time** (when your pipeline sees it). They diverge — Akidau's [Streaming 101](https://www.oreilly.com/radar/the-world-beyond-batch-streaming-101/) calls the gap *skew*, and it is unbounded in practice: a phone goes through a tunnel, buffers ten minutes of clicks, and flushes them all at once. Those clicks arrive late *and out of order*.

Window by processing time and your "12:00–12:01" bucket contains whatever happened to arrive then — cheap, and fine for metrics like requests-per-second *observed*. But if the question is about when things *happened* — billing, sessionization, fraud — you must window by event time, and then you inherit the completeness problem: how long do you wait for stragglers?

## Where: three window shapes

- **Tumbling (fixed)**: contiguous, non-overlapping. `[12:00, 12:01)`, `[12:01, 12:02)`. Each event lands in exactly one window. Per-minute click counts.
- **Hopping (sliding)**: fixed size, smaller advance. Size 1 min, hop 10 s → each event lands in 6 windows. Smooth moving averages; storage/compute cost multiplies by `size/hop`.
- **Session**: data-driven — a window per key that grows while events keep arriving within a *gap timeout* and closes after silence. Sessions have no aligned edges, which is why the Dataflow paper treats windows as merging per-key state rather than a static bucketing.

## When: what a watermark asserts

A **watermark** is a moving event-time threshold flowing through the pipeline. Watermark `W(t)` is the system asserting: *"I believe all events with event time ≤ t have arrived."* When the watermark passes a window's end, the window is complete-as-far-as-we-know and can fire and (eventually) free its state.

- A **perfect watermark** makes that assertion a guarantee. Possible only when the source can promise ordering per partition (e.g., a log you control end-to-end). Nothing is ever late; you may wait a long time.
- A **heuristic watermark** is an educated guess — typically "max event time seen, minus a bounded delay." [Streaming 102](https://www.oreilly.com/radar/the-world-beyond-batch-streaming-102/) is blunt about the trade-off: too slow and results lag; too fast and data arrives *behind* the watermark. That data is **late**.

So the watermark delay is your latency/completeness dial, and late data is not an edge case — it is the designed-for consequence of choosing a heuristic. (Begoli et al.'s [VLDB 2021 comparison](http://www.vldb.org/pvldb/vol14/p3135-begoli.pdf) shows Flink and Dataflow implement the same idea with different propagation mechanics.)

## Late data, triggers, accumulation

Three sanctioned fates for a late event: **drop** it, **reprocess** it by re-firing the window (bounded by an *allowed lateness* horizon, after which state is garbage-collected), or **divert** it to a side output / correction stream. Triggers generalize "fire at watermark": you can also fire early (speculative partial results every N seconds) and fire again on late data. Each re-firing needs an **accumulation mode** — *discarding* (emit only the delta), *accumulating* (emit the updated total; downstream must upsert by window key), or *accumulating & retracting* (emit a retraction plus the new value, for consumers that re-aggregate).

## The example: late clicks in Flink SQL

```sql
CREATE TABLE clicks (
  user_id  STRING,
  url      STRING,
  ts       TIMESTAMP(3),
  -- heuristic watermark: trust arrival to be at most 10s out of order
  WATERMARK FOR ts AS ts - INTERVAL '10' SECOND
) WITH ('connector' = 'kafka', 'topic' = 'clicks', ...);

-- per-user clicks per minute, tumbling event-time windows
SELECT window_start, window_end, user_id, COUNT(*) AS clicks
FROM TABLE(
  TUMBLE(TABLE clicks, DESCRIPTOR(ts), INTERVAL '1' MINUTE))
GROUP BY window_start, window_end, user_id;
```

Walk the timeline for window `[12:00, 12:01)`:

1. Clicks with `ts` 12:00:05 and 12:00:58 arrive in order — buffered into the window.
2. At wall time 12:01:04 an event with `ts = 12:00:59` arrives. The watermark is only `max_ts − 10s ≈ 12:00:49`, which is **before** the window end — so this out-of-order click is *not late*; it's counted normally. This is the case the 10-second delay buys you.
3. An event with `ts = 12:00:58` arriving after the watermark has passed 12:01:00 **is** late. Flink SQL window TVFs drop it (watch the `numLateRecordsDropped` metric). If you need the reprocess-or-divert behavior, that's the DataStream API: `.window(...).allowedLateness(Time.minutes(5)).sideOutputLateData(tag)` re-fires the window per late event (accumulating mode) and diverts anything beyond 5 minutes.

Kafka Streams expresses the same dial as a **grace period** — `TimeWindows.ofSizeAndGrace(ofMinutes(1), ofSeconds(10))` — with updates emitted per record and `suppress()` to hold output until the window closes.

## The interview checklist

For "aggregate a click stream," state your choices explicitly: event-time tumbling windows keyed by user; watermark = max event time minus a delay you pick from measured p99 lateness; late data beyond that dropped to a correction topic or handled by allowed lateness; accumulating output into an upsertable sink keyed by `(window, user)`. Then name the costs: watermark delay adds latency to every result, allowed lateness holds per-window state longer, and session windows make all of it per-key. If they push on delivery guarantees, that's a different topic — see [exactly-once in Kafka](/articles/microservices/2026-08-13-exactly-once-delivery-semantics-kafka) — and if ingestion outpaces the aggregator, that's [backpressure](/articles/sys-patterns/2026-07-31-backpressure-flow-control), not windowing.

**Try next:** Run the Flink SQL above against a local Kafka topic and replay events with shuffled timestamps; watch `numLateRecordsDropped` as you shrink the watermark interval from 10s to 1s.
