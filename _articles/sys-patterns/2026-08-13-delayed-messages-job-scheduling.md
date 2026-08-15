---
title: "Delayed messages and job scheduling: why Kafka cannot delay, and what to use instead"
date: 2026-08-13
track: sys-patterns
summary: "\"Deliver this in 30 minutes\" has no native implementation on Kafka, because a log is not a priority queue. The working options: delay-tier topics with paused consumers, SQS delays and visibility timeouts, Redis sorted sets, and a Postgres queue with FOR UPDATE SKIP LOCKED — plus the timer-wheel structure brokers use internally."
reading_time: 7
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

**Gist.** Deferred delivery — retry a payment in ten minutes, send a reminder tomorrow at nine — requires a per-message due time, and an append-only log has no index on due times. The workable mechanisms either quantise the delay into a small number of fixed tiers whose consumers pause and resume, or move the schedule into a store with an ordered index on the due timestamp (Amazon Simple Queue Service (SQS) delay and visibility timers, a Redis sorted set, a relational table). The cost is either precision, in the quantised case, or an extra durable store on the critical path that must be polled, claimed and swept when workers die.

## Why a Kafka partition cannot hold a message back

A Kafka partition is an append-only log consumed in offset order. Delaying one record admits only two outcomes: hold back every record behind it, which is head-of-line blocking for the whole partition, or deliver out of offset order, which breaks the contract that consumer position, replication and offset commits are built on. **There is no per-message timer index in the broker**, so there is no structure to hang a delay on. No shipped broker feature supplies deferred delivery, so delay is an application-level pattern.

## Delay-tier topics with pause and resume

The established Kafka construction, described in [Uber's reprocessing pipeline](https://www.uber.com/blog/reliable-reprocessing/), is a small set of topics with fixed delay tiers — `retry-1m`, `retry-10m`, `retry-1h` — with each message routed to the tier nearest its due time. A tier's consumer reads a record, computes `due = record.timestamp + tier_delay`, and if `due` is in the future calls `consumer.pause(partitions)`, waits until `due`, then calls `resume()`.

Two invariants make this correct. First, **a paused consumer still calls `poll()`**, which keeps the group-membership heartbeat alive and avoids eviction for exceeding `max.poll.interval.ms`; a consumer that sleeps without pausing is removed from the group and its partitions are reassigned mid-wait. Second, **every message in a tier carries the same delay**, so records arrive in due-time order within the partition and blocking on the head record never starves a later one that was already due. Records that keep failing cascade to coarser tiers and finally to a dead-letter queue.

The costs: delay precision is quantised to the tier set, each tier is an additional topic and consumer group to operate, and arbitrary per-message delays would require an impractical number of tiers. The construction scales the way Kafka scales, which is the reason to choose it.

## Timer wheels: tracking many timers at O(1) insertion

Kafka needs timeouts internally — a produce request with `acks=all` waits in *purgatory* until replication completes or the request expires. A priority queue costs O(log n) per insertion and removal. Kafka instead uses **hierarchical timing wheels**, the structure from [Varghese and Lauck's 1987 SOSP paper](https://dl.acm.org/doi/10.1145/41457.37504).

A *hashed* wheel is a circular array of buckets, one per tick. Insertion is `buckets[(now + delay) mod size]`, O(1), with no comparison against other pending timers. Timers beyond the wheel's horizon overflow into a higher-level wheel with coarser ticks — seconds, then minutes, then hours; when a coarse bucket expires, its timers are re-inserted into the finer wheel below, so a timer descends through the hierarchy and is only ever compared against the resolution it currently occupies. [Kafka's purgatory](https://www.confluent.io/blog/apache-kafka-purgatory-hierarchical-timing-wheels/) enqueues whole *buckets* into a `java.util.concurrent.DelayQueue`, so the driving thread wakes once per due bucket rather than once per timer. Confluent reports the redesign sustaining a materially higher request rate than the earlier queue-based purgatory; the published figures are from a single synthetic benchmark and are not a bound.

### Implementation sketch (Scala)

A single hashed wheel, showing the load-bearing operations: constant-time insertion by modular bucket index, and a tick that drains exactly one bucket. Overflow to a coarser wheel is the recursive extension, elided here.

```scala
final class HashedWheel[A](val tickMs: Long, val size: Int):
  private val buckets = Array.fill(size)(List.empty[(Long, A)])
  private var currentTick: Long = 0L

  /** O(1): the bucket is chosen arithmetically, not by comparison. */
  def schedule(delayMs: Long, task: A): Unit =
    val ticks    = math.max(delayMs / tickMs, 0L)
    val deadline = currentTick + ticks
    val idx      = (deadline % size).toInt
    buckets(idx) = (deadline, task) :: buckets(idx)

  /** Advance one tick and return the tasks due now. Entries whose deadline is
    * a full revolution away stay put: the same slot serves many rounds. */
  def advance(): List[A] =
    currentTick += 1
    val idx = (currentTick % size).toInt
    val (due, later) = buckets(idx).partition(_._1 <= currentTick)
    buckets(idx) = later
    due.map(_._2)
```

The `partition` on revisit is what keeps a fixed-size wheel able to hold deadlines beyond `size` ticks; a hierarchical wheel replaces it by promoting those entries to a coarser wheel instead of re-scanning them each revolution.

## SQS: enqueue delay and visibility timeout

[SQS exposes two timers](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-delay-queues.html). `DelaySeconds`, in the range 0–900 seconds (a maximum of 15 minutes), hides a message when it is *enqueued*; it is set per queue, or per message via message timers, which are available on standard queues only — FIFO queues support the queue-level setting alone. The **visibility timeout** hides a message after it is *received*: if the consumer does not delete it before the timeout elapses, the message becomes visible again and is redelivered.

That second timer makes retry with backoff cheap: on failure the consumer calls `ChangeMessageVisibility` with an exponentially growing value, up to 12 hours, and does not delete the message. Delays beyond 15 minutes are outside what a delay queue expresses and need an external scheduler holding the due time.

## The RabbitMQ delayed exchange

The `x-delayed-message` plugin stores delayed messages in a **single Mnesia replica on one node**; loss of that node, or disabling the plugin, loses every undelivered delayed message. The README states the plugin is unsuitable for large numbers of pending messages or for long delays, and the [project is no longer maintained by Team RabbitMQ](https://github.com/rabbitmq/rabbitmq-delayed-message-exchange).

The supported native alternative is dead-lettering with a per-message or per-queue time-to-live (TTL): publish to a wait queue that has a TTL and no consumers, and let expired messages dead-letter into the real queue. This carries the same tiering caveat as the Kafka construction, because **TTL expiry is evaluated at the queue head** — a message with a short TTL behind one with a long TTL is not released early.

## Sorted sets and databases as schedulers

Arbitrary per-job times require a store with an ordered index on the due timestamp.

**Redis:** `ZADD sched <due_epoch> <job>` places the job in a sorted set keyed by due time; a poller runs a Lua script that pops entries with `score <= now` atomically, or issues `ZPOPMIN` and re-adds the entry when its score proves to be in the future. This is the mechanism Sidekiq uses to schedule jobs.

**Postgres**, polled for due rows and claiming them without blocking peers:

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

**`SKIP LOCKED` is the load-bearing clause**: a worker passes over rows another worker's transaction has locked instead of queueing behind them, so N workers drain the table concurrently rather than serialising on the oldest due row. Completion marks the row `done` or `failed`, with a new `run_at` when the failure is retried with backoff, and a sweeper must return `running` rows whose worker died to `pending`.

## Choosing

| Approach | Precision | Scale | Ops cost |
|---|---|---|---|
| Kafka delay-tier topics + pause/resume | Quantised to tiers | Very high (Kafka's own) | Topic + consumer per tier |
| SQS DelaySeconds / message timers | Seconds, ≤15 min | High, managed | Near zero |
| SQS visibility-timeout backoff | Seconds, ≤12 h per hop | High, managed | Near zero |
| RabbitMQ delayed-exchange plugin | ms–hours | Low (single-node store) | Unmaintained — avoid |
| Redis sorted set + poller | ~poll interval (sub-second) | High (memory-bound) | Redis plus a self-operated poller |
| Postgres `SKIP LOCKED` queue | ~poll interval (seconds) | Moderate (one table's write rate) | Existing database only |

Jobs whose loss matters belong where the transactions already are — a relational table — or in a managed queue with durable timers. Delay-tier topics fit traffic that is already Kafka-shaped. Timer wheels are the answer when the question concerns the scheduler's own implementation.

## Pitfalls

- **A consumer that sleeps without calling `consumer.pause()` is evicted from the group.** No `poll()` within `max.poll.interval.ms` triggers a rebalance, the partitions are reassigned, and the delayed record is redelivered to another member that sleeps in turn.
- **Mixed delays in one tier reintroduce head-of-line blocking.** The tier's invariant is a uniform delay; routing a one-hour message into `retry-1m` makes every record behind it wait an hour.
- **Message timers on a FIFO queue are silently unavailable.** SQS FIFO queues support only the queue-level `DelaySeconds`, so per-message delay values do not take effect there.
- **`DelaySeconds` caps at 900 seconds.** A request for a longer delay is rejected rather than silently accepted at the intended time.
- **Losing the node holding the RabbitMQ delayed-message store loses every pending delayed message.** The plugin keeps a single Mnesia replica; disabling the plugin has the same effect.
- **Dropping `SKIP LOCKED` converts a parallel claim into a serial one.** Each worker's `FOR UPDATE` blocks on the oldest locked due row, so throughput collapses to one worker regardless of how many are running.
- **Jobs claimed by a worker that crashes stay in `running` forever.** The claim transaction commits before execution, so nothing releases the row; a sweeper keyed on claim age is required.
- **A poller without an index on the due column scans the whole table each cycle.** The partial index on `run_at` restricted to `state = 'pending'` is what keeps the claim query proportional to the due backlog rather than to table size.
