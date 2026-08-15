---
title: "The work queue pattern: one queue, a pool of disposable workers"
date: 2026-07-26
track: sys-patterns
summary: "Brendan Burns' work-queue pattern turns batch computation into a shared queue plus a pool of stateless workers — a reusable source/worker container framework, horizontal scaling, and crash-safe visibility timeouts."
reading_time: 7
tags: [work-queue, batch, workers, burns, idempotency, visibility-timeout, kubernetes]
sources:
  - title: "Designing Distributed Systems, 2nd Edition — Brendan Burns (O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Design Patterns for Container-Based Distributed Systems — Burns & Oppenheimer (HotCloud '16)"
    url: "https://research.google.com/pubs/archive/45406.pdf"
  - title: "Coarse Parallel Processing Using a Work Queue — Kubernetes docs"
    url: "https://kubernetes.io/docs/tasks/job/coarse-parallel-processing-work-queue"
  - title: "Amazon SQS visibility timeout — AWS documentation"
    url: "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html"
---

**Gist.** Batch workloads have no request/response shape: a finite pile of independent tasks must each be computed once, and the computation must survive workers dying mid-task. The **work queue** pattern places every task in one shared queue and drains it with a pool of interchangeable, stateless workers, so throughput scales by adding workers and crash recovery reduces to redelivering an unacknowledged item. The cost is that redelivery is the recovery mechanism, which forces **at-least-once delivery** and therefore requires every task to be idempotent.

Brendan Burns treats this as the canonical *batch computational* pattern in *Designing Distributed Systems*. The distinctive part of his treatment is not the queue but the decomposition of the worker into reusable containers — the same modular instinct behind the sidecar and ambassador patterns described with Oppenheimer in the HotCloud '16 paper.

## The pattern: one queue, many stateless workers

Three moving parts:

- A **queue** holding self-contained *work items* (a URL, a row identifier, a JSON blob).
- A pool of **workers**, each running one loop: pull an item, process it, acknowledge it, repeat.
- A **source** that fills the queue.

The load-bearing invariant is that **workers hold no state between items and have no knowledge of one another**. Any worker can handle any item, so throughput scales by the crudest available lever — start more workers — and the queue itself performs load balancing by handing each pull to whichever worker asked. There is no leader to elect because no worker occupies a distinguished role, so the coordination cost that dominates stateful systems is absent.

A second consequence is rate decoupling. The queue is a buffer between producer and consumer, so a burst arriving in ten seconds may drain over ten minutes; the depth of the queue absorbs the difference rather than the system shedding work. The bound is storage: the queue must hold the peak backlog, and once retention or depth limits are hit the pattern degrades to dropping or blocking the producer.

## Burns's decomposition: a reusable framework, pluggable parts

Burns's argument is that the machinery should not be rewritten per job. The queue-draining loop, the retry logic and the parallelism management are identical across batch workloads, so they belong in a **reusable framework** exposing two container-shaped interfaces into which job-specific logic is plugged:

| Container | Responsibility | Reused? |
|-----------|----------------|---------|
| **Source** | Produces the stream of work items behind a shared interface (for example an HTTP endpoint returning the next item) | Swappable per job |
| **Worker** | Consumes one item and performs the computation | Swappable per job |
| **Framework** | Coordinates: polls the source, dispatches to workers, tracks completion, handles retries | Written once, reused everywhere |

In the container formulation each worker is paired with an **ambassador** container speaking a generic queue application programming interface (API). The worker issues a plain "next item" call; the ambassador translates it to Amazon Simple Queue Service (SQS), RabbitMQ, or a Redis list. The practical effect is a boundary: the worker author writes business logic against a fixed interface, and the choice of queue technology becomes a deployment decision rather than a code change.

## A worker loop that survives crashes

The failure mode that defines the pattern is that **a worker will die mid-item**: the pod is evicted, the process exhausts memory, the network partitions. If a pull removes the item permanently, that work is lost with no record that it existed.

The mechanism against this is the **visibility timeout** (the SQS term; RabbitMQ expresses the same idea through acknowledge/negative-acknowledge, and a plain Redis list has no equivalent). Receiving an item does not delete it — it hides the item for N seconds. Deletion happens only after processing completes. If the worker dies first, the timeout lapses and the item becomes visible again for another worker.

The item therefore moves through a three-state machine: **visible → invisible (leased for N seconds) → deleted**, with the only transition out of the middle state other than deletion being a timeout back to *visible*. Nothing in this machine detects a crash; it detects the absence of a deletion within the lease.

```python
import boto3

sqs = boto3.client("sqs")
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/work"

def process(item_id: str) -> None:
    # The job-specific computation. MUST be idempotent — see below.
    render_invoice(item_id)

while True:
    resp = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=1,
        WaitTimeSeconds=20,        # long poll, no busy-spin
        VisibilityTimeout=120,     # item is hidden for 2 minutes
    )
    for msg in resp.get("Messages", []):
        try:
            process(msg["Body"])
            # Only now is the item truly gone.
            sqs.delete_message(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=msg["ReceiptHandle"],
            )
        except Exception:
            # No deletion. The timeout lapses and the item is redelivered.
            # Setting the timeout to zero makes the item visible immediately:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=msg["ReceiptHandle"],
                VisibilityTimeout=0,
            )
```

Two rules make the loop safe:

1. **Delete last.** The acknowledgement follows durable completion of the work; acknowledging on receipt converts every crash into lost work.
2. **Be idempotent.** Mainstream queues provide *at-least-once* delivery. The SQS documentation describes standard queues as storing message copies on multiple servers, so a copy that was not deleted on a temporarily unavailable server can be received again. A redelivered item must therefore produce the same result — achieved by using the item identifier as a database primary key, by an upsert, or by a `processed_ids` set consulted before the side effect. Idempotency is what makes redelivery cheap rather than corrupting.

The visibility timeout is a tuning parameter with a failure mode on each side. **Too short**, and a worker that is slow but alive loses its lease while still running: the item is redelivered and processed concurrently by two workers. **Too long**, and a genuine crash leaves the item invisible for the full lease before any retry begins. Sizing it to observed high-percentile processing time plus headroom bounds the first failure at the tail of the latency distribution. SQS defaults to 30 seconds with a maximum of 12 hours.

### Implementation sketch (Scala)

A minimal framework in the Burns sense: the loop, the lease and the retry live here; `Source` and `Worker` are the pluggable parts.

```scala
trait Source[A]:
  /** None when no item is currently available. */
  def lease(visibility: FiniteDuration): Option[Leased[A]]

final case class Leased[A](item: A, receipt: String, deadline: Instant)

trait Sink:
  def delete(receipt: String): Unit
  def release(receipt: String): Unit   // make visible again immediately

trait Worker[A]:
  def process(item: A): Unit           // must be idempotent

final class Framework[A](src: Source[A], sink: Sink, w: Worker[A]):
  def drainOnce(visibility: FiniteDuration): Boolean =
    src.lease(visibility) match
      case None => false
      case Some(Leased(item, receipt, _)) =>
        try
          w.process(item)
          sink.delete(receipt)          // acknowledge only after completion
        catch case NonFatal(_) => sink.release(receipt)
        true

  def run(visibility: FiniteDuration, parallelism: Int): Unit =
    val threads = (1 to parallelism).map: _ =>
      Thread.startVirtualThread(() => while drainOnce(visibility) do ())
    threads.foreach(_.join())
```

The workers share nothing, so `parallelism` is a plain integer and no rebalancing step exists.

## Scaling it: a Kubernetes Job with parallelism

Kubernetes implements this pattern natively. A `Job` with `parallelism: N` runs N worker pods against one queue, and the emptiness of the queue — not a completion count — is the termination condition. This is the coarse parallel work queue documented in the [Kubernetes docs](https://kubernetes.io/docs/tasks/job/coarse-parallel-processing-work-queue):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: batch-workers
spec:
  parallelism: 8          # eight workers draining concurrently
  # completions is omitted: the Job completes once one pod exits 0 and the rest have terminated
  template:
    spec:
      restartPolicy: OnFailure   # crashed pod restarts, re-pulls work
      containers:
        - name: worker
          image: registry.example.com/invoice-worker:1.4
          env:
            - name: QUEUE_URL
              value: "amqp://rabbitmq.default.svc/work"
```

Raising `parallelism` from 8 to 40 multiplies worker count by five with no code change and no rebalancing step, because the queue is the only point of coordination. `restartPolicy: OnFailure` closes the crash loop at the orchestration layer: a dead pod is restarted and re-pulls whatever the visibility timeout has released. Framework and workers, as Burns draws them.

## Pitfalls

- **Acknowledging on receipt instead of on completion.** Symptom: items vanish with no output and no error; cause: the deletion happened before the side effect, so a crash between the two leaves no trace of the item.
- **A non-idempotent `process`.** Symptom: duplicate invoices, doubled counters, repeated outbound emails; cause: at-least-once delivery redelivers on every lease expiry, and the second run repeats the side effect.
- **A visibility timeout shorter than the processing tail.** Symptom: two workers run the same item concurrently under normal operation, with no crash involved; cause: the lease expired while the first worker was still running.
- **A visibility timeout far longer than processing time.** Symptom: after a pod eviction the backlog stalls; cause: the item stays invisible for the remainder of the lease before any worker can see it.
- **Assuming `restartPolicy: OnFailure` recovers the item.** Symptom: the restarted pod idles while its previous item is still hidden; cause: pod restart and lease expiry are independent timers, and the item returns only when the visibility timeout lapses.
- **Treating queue emptiness as completion while producers still run.** Symptom: workers exit 0 and the Job reports success with tasks unprocessed; cause: a momentarily empty queue is indistinguishable from an exhausted one unless the source signals end of stream.
- **Building the pattern on a plain Redis list without a reaper.** Symptom: items accumulate in per-worker processing lists and are never retried; cause: `BRPOPLPUSH` moves an item out of `pending` atomically but nothing returns it there when the worker dies — the timeout sweep has to be written by hand.
