---
title: "Delayed messages and job scheduling: why Kafka can't wait, and what to use instead"
date: 2026-08-13
track: sys-patterns
summary: "\"Send this in 30 minutes\" sounds trivial until you try it on Kafka, which has no native delay because a log is not a priority queue. The working options: delay-tier topics with paused consumers, SQS delays and visibility timeouts, Redis sorted sets, and a Postgres queue with FOR UPDATE SKIP LOCKED — plus the timer-wheel data structure brokers use internally."
reading_time: 6
tags: [delayed-messages, kafka, job-scheduling, timer-wheels, skip-locked]
sources:
  - title: "Varghese & Lauck — Hashed and Hierarchical Timing Wheels (SOSP 1987)"
    url: "https://dl.acm.org/doi/10.1145/41457.37504"
  - title: "Confluent — Apache Kafka, Purgatory, and Hierarchical Timing Wheels"
    url: "https://www.confluent.io/blog/apache-kafka-purgatory-hierarchical-timing-wheels/"
  - title: "AWS — Amazon SQS delay queues"
    url: "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-delay-queues.html"
  - title: "Uber Engineering — Building Reliable Reprocessing and Dead Letter Queues with Apache Kafka"
    url: "https://www.uber.com/blog/reliable-reprocessing/"
  - title: "rabbitmq-delayed-message-exchange — README and maintenance notice (GitHub)"
    url: "https://github.com/rabbitmq/rabbitmq-delayed-message-exchange"
---

"Retry this payment in 10 minutes." "Send the reminder email tomorrow at 9." Every system grows a delayed-delivery requirement, and the interview version of the question is usually: *you're on Kafka — now what?*

## Why Kafka can't just delay a message

A Kafka partition is an append-only log consumed in offset order. Delaying one message means either holding back every message behind it (head-of-line blocking) or delivering out of offset order — which would break the core contract consumers, replication, and offset commits are built on. There's no per-message timer index in the broker, so there's nothing to hang a delay on. (KIP-1277 proposes a `deliverAfter` field with a broker-side delivery-time index, but it's still under discussion and explicitly scoped to short delays.) Until something like that lands, delay is an application-level pattern.

## Delay-tier topics + pause/resume

The standard Kafka answer, popularized by [Uber's reprocessing pipeline](https://www.uber.com/blog/reliable-reprocessing/): create a small set of topics with fixed delay tiers — `retry-1m`, `retry-10m`, `retry-1h` — and route each message to the tier closest to its due time. Each tier's consumer reads a message, computes `due = record.timestamp + tier_delay`, and if `due` is in the future it calls `consumer.pause(partitions)`, sleeps (or sets a timer) until due, then `resume()`s. Pausing keeps `poll()` legal so the consumer isn't kicked from the group for exceeding `max.poll.interval.ms`, and because every message in the tier has the *same* delay, waiting on the head message never starves a later one. Messages that keep failing cascade down the tiers and land in a DLQ.

Trade-offs: delay precision is quantized to your tiers, each tier is an extra topic + consumer to operate, and arbitrary per-message delays need many tiers. It scales exactly like Kafka does, though, which is the point.

## Timer wheels: how a broker tracks a million timers

Kafka itself needs timeouts everywhere — a produce request with `acks=all` waits in "purgatory" for replication or expiry. Tracking that with a priority queue costs O(log n) per operation; Kafka instead uses **hierarchical timing wheels**, the data structure from [Varghese & Lauck's 1987 SOSP paper](https://dl.acm.org/doi/10.1145/41457.37504). A *hashed* wheel is a circular array of buckets, one per tick: insert = `buckets[(now + delay) mod size]`, O(1). Timers beyond the wheel's horizon overflow into a higher-level wheel with coarser ticks (seconds → minutes → hours); when a coarse bucket expires, its timers are re-inserted into the finer wheel below. [Kafka's purgatory](https://www.confluent.io/blog/apache-kafka-purgatory-hierarchical-timing-wheels/) enqueues whole *buckets* into a `java.util.concurrent.DelayQueue` so a thread wakes only when a bucket is due — the redesign held ~105k req/s where the old queue-based purgatory saturated around 40k. Timer wheels are the right mention when an interviewer asks "how would you implement the scheduler itself?"

## SQS: delays and visibility timeouts

If you're on AWS, [SQS gives you two knobs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-delay-queues.html). `DelaySeconds` (0–900s, i.e. max 15 minutes) hides a message when it's *enqueued* — per queue, or per message via message timers (standard queues only; FIFO supports only the queue-level setting). The **visibility timeout** hides a message after it's *received*; if the consumer doesn't delete it in time, it reappears. That makes retry-with-backoff nearly free: on failure, call `ChangeMessageVisibility` with an exponentially growing value (up to 12 hours) and simply don't delete the message. For delays beyond 15 minutes AWS points you at EventBridge Scheduler rather than chaining delay queues.

## RabbitMQ's delayed exchange: a cautionary tale

RabbitMQ's `x-delayed-message` exchange plugin is the classic example of "plugin ≠ platform feature." Delayed messages sit in a **single Mnesia replica on one node** — lose that node (or disable the plugin) and every undelivered delayed message is gone. The README warns it's unsuitable for hundreds of thousands of pending messages or delays beyond a day or two, and as of 2026 the [project is no longer maintained by Team RabbitMQ](https://github.com/rabbitmq/rabbitmq-delayed-message-exchange) (Mnesia was removed in RabbitMQ 4.3.0). The supported native alternative is dead-letter + per-message/queue TTL: publish to a wait queue with a TTL and no consumers; expired messages dead-letter into the real queue — same tiering caveat as Kafka, since TTL expiry is checked at the queue head.

## Sorted sets and databases as schedulers

For arbitrary per-job times, use a store with an ordered index on `due_time`. **Redis:** `ZADD sched <due_epoch> <job>`; a poller runs a Lua script (or `ZPOPMIN` + check) to atomically pop entries with `score <= now` — this is how Sidekiq schedules jobs. **Postgres (Quartz-style polling, minus Quartz's row-lock contention):**

```sql
CREATE TABLE jobs (
  id       bigserial PRIMARY KEY,
  run_at   timestamptz NOT NULL,
  payload  jsonb NOT NULL,
  state    text NOT NULL DEFAULT 'pending',
  attempts int  NOT NULL DEFAULT 0
);
CREATE INDEX jobs_due_idx ON jobs (run_at) WHERE state = 'pending';

-- Each worker, in a transaction, claims due jobs without blocking peers:
WITH due AS (
  SELECT id FROM jobs
  WHERE state = 'pending' AND run_at <= now()
  ORDER BY run_at
  LIMIT 10
  FOR UPDATE SKIP LOCKED
)
UPDATE jobs j SET state = 'running', attempts = attempts + 1
FROM due WHERE j.id = due.id
RETURNING j.id, j.payload;
```

`SKIP LOCKED` is the load-bearing clause: workers skip rows another worker has locked instead of queueing behind them, so N workers drain the table in parallel. Mark `done`/`failed` (with a new `run_at` for backoff) on completion, and sweep `running` rows whose worker died.

## Choosing

| Approach | Precision | Scale | Ops cost |
|---|---|---|---|
| Kafka delay-tier topics + pause/resume | Quantized to tiers | Very high (it's Kafka) | Topic + consumer per tier |
| SQS DelaySeconds / message timers | Seconds, ≤15 min | High, managed | Near zero |
| SQS visibility-timeout backoff | Seconds, ≤12 h per hop | High, managed | Near zero |
| RabbitMQ delayed-exchange plugin | ms–hours | Low (single-node store) | Unmaintained — avoid |
| Redis sorted set + poller | ~poll interval (sub-second) | High (memory-bound) | Redis + poller you own |
| Postgres `SKIP LOCKED` queue | ~poll interval (seconds) | Moderate (one table's write rate) | Just your database |

Default answer: if the jobs matter, put them where your transactions already are (Postgres) or in a managed queue (SQS); reach for delay-tier topics when the traffic is already Kafka-shaped; mention timer wheels when the question is about building the scheduler itself.

**Try next:** run two `psql` sessions against the jobs table above and watch `FOR UPDATE SKIP LOCKED` hand each session disjoint rows — then drop `SKIP LOCKED` and watch the second session block.
