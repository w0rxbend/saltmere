---
title: "The work queue pattern: one queue, a pool of disposable workers"
date: 2026-07-26
track: sys-patterns
summary: "How Brendan Burns' work-queue pattern turns batch computation into a shared queue plus a pool of stateless workers — with a reusable source/worker container framework, horizontal scaling, and crash-safe visibility timeouts."
reading_time: 5
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

Some workloads have no request/response. You have a pile of things to compute — thumbnails to resize, invoices to render, feature vectors to extract — and you want them done fast and done once. The **work queue** is the pattern for exactly this: put every task in a shared queue, and let a pool of interchangeable workers race to drain it.

Brendan Burns treats this as the canonical *batch computational* pattern in *Designing Distributed Systems*. What makes his version worth studying isn't the queue — it's how he decomposes the worker into reusable containers, the same object-oriented instinct behind the sidecar and ambassador patterns.

## The pattern: one queue, many stateless workers

Three moving parts:

- A **queue** holding self-contained *work items* (a URL, a row ID, a JSON blob).
- A pool of **workers**, each a loop: pull an item, process it, acknowledge it, repeat.
- A **source** that fills the queue.

The workers hold no state between items and don't know about each other. That's the whole trick. Because any worker can handle any item, you scale throughput by the crudest possible lever — run more workers — and the queue load-balances for you. This is horizontal scaling with none of the coordination cost of stateful systems; there is no leader to elect because no worker is special.

The queue also decouples producer rate from consumer rate. A burst that arrives in ten seconds can drain over ten minutes, and the buffer absorbs the difference instead of dropping work.

## Burns's decomposition: a reusable framework, pluggable parts

Burns's real argument is that you should never rewrite this machinery per job. The queue-draining loop, retry logic, and parallelism management are identical across every batch workload — so package them as a **reusable framework** and expose two container-shaped interfaces you plug your logic into:

| Container | Responsibility | Reused? |
|-----------|----------------|---------|
| **Source** | Produces the stream of work items on a shared interface (e.g. an HTTP endpoint returning the next item) | Swappable per job |
| **Worker** | Consumes one item and does the actual computation | Swappable per job |
| **Framework** | Coordinates: polls the source, dispatches to workers, tracks completion, handles retries | Written once, reused everywhere |

In the container version he pairs each worker with an **ambassador** that speaks a generic queue API — so the worker calls a plain "give me the next item" interface while the ambassador translates to SQS, RabbitMQ, or a Redis list underneath. As one reviewer summarizes his philosophy: *"use collections of small containers together... to allow any individual container to have maximum focus and reusability."* The worker author writes only business logic against a fixed interface; the queue technology is a deployment detail.

## A worker loop that survives crashes

The uncomfortable truth of any queue: **a worker will die mid-item.** The pod gets evicted, the process OOMs, the network partitions. If pulling an item removes it permanently, that work is lost.

The fix is the **visibility timeout** (SQS's term; RabbitMQ calls it ack/nack, Redis needs you to build it). Receiving an item doesn't delete it — it *hides* it for N seconds. You delete it only after you've finished. If you crash first, the timeout lapses and the item reappears for another worker.

```python
import boto3

sqs = boto3.client("sqs")
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/123456789012/work"

def process(item_id: str) -> None:
    # Your actual work. MUST be idempotent — see below.
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
            # Don't delete. Timeout lapses; item is redelivered.
            # Optionally shorten the timeout to retry sooner:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=msg["ReceiptHandle"],
                VisibilityTimeout=0,
            )
```

Two rules make this safe:

1. **Delete last.** Acknowledge only after the work is durably done, never on receipt.
2. **Be idempotent.** Every mainstream queue gives you *at-least-once* delivery — AWS states plainly that with the at-least-once model, "Amazon SQS doesn't guarantee that a message won't be delivered more than once." A redelivered item must produce the same result: use the item ID as a database primary key, an upsert, or a `processed_ids` set. Idempotency is not optional here; it's what makes redelivery cheap instead of corrupting.

Pick the visibility timeout deliberately. Too short and a slow-but-alive worker gets its item stolen and processed twice; too long and a genuine crash sits undetected. Size it to your P99 processing time plus headroom (SQS defaults to 30s, max 12h).

## Scaling it: a Kubernetes Job with parallelism

Kubernetes ships this pattern natively. A `Job` with `parallelism: N` runs N worker pods against one queue; the queue's emptiness — not a completion count — signals "done." This is the coarse parallel work queue from the [Kubernetes docs](https://kubernetes.io/docs/tasks/job/coarse-parallel-processing-work-queue):

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: batch-workers
spec:
  parallelism: 8          # eight workers draining concurrently
  completions: null       # done when a worker sees an empty queue and exits 0
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

Bump `parallelism` from 8 to 40 and you have 5x the throughput — no code change, no rebalancing. `restartPolicy: OnFailure` closes the crash loop at the orchestration layer: a dead pod restarts and re-pulls whatever the visibility timeout has released. Framework and workers, exactly as Burns draws it.

**Try next:** Build the pattern on a bare Redis list to feel what SQS gives you for free. Use `BRPOPLPUSH` to atomically move an item from a `pending` list to a per-worker `processing` list, run the work, then `LREM` it on success. Add a reaper that scans `processing` lists for items older than your timeout and pushes them back to `pending` — and you'll have hand-built visibility timeouts from primitives.
