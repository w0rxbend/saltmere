---
title: "Dead letter queues: containing poison messages"
date: 2026-07-30
track: microservices
summary: "A single un-processable message can retry indefinitely and block the messages behind it in an ordered log. A dead letter queue quarantines it after a bounded number of attempts so the stream continues. Covers the attempt-counting mechanic, a RabbitMQ dead-letter-exchange retry topology, and the ordering cost."
reading_time: 6
tags: [dead-letter-queue, poison-message, rabbitmq, kafka, resiliency, retries]
sources:
  - title: "Dead Letter Exchanges — RabbitMQ Documentation"
    url: "https://www.rabbitmq.com/docs/dlx"
  - title: "Building Microservices, 2nd ed. (resiliency & DLQs) — Sam Newman"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Error Handling Patterns for Apache Kafka Applications — Confluent Blog"
    url: "https://www.confluent.io/blog/error-handling-patterns-in-kafka/"
  - title: "Kafka Connect Deep Dive – Error Handling and Dead Letter Queues — Confluent Blog"
    url: "https://www.confluent.io/blog/kafka-connect-deep-dive-error-handling-dead-letter-queues/"
---

**Gist.** At-least-once delivery guarantees that a message is redelivered until acknowledged, which becomes a livelock when a message can never be processed — a malformed payload, a schema the consumer cannot decode, a referenced record that no longer exists. A **dead letter queue (DLQ)** bounds the damage: after a fixed number of failed attempts the message is republished to a separate destination and acknowledged off the live queue, so the consumer advances. The cost is **loss of total order** — a message diverted to the DLQ is overtaken by its successors, which is a correctness defect whenever a later message depends on an earlier one.

## The failure mode

A **poison message** is one whose processing fails deterministically. The distinguishing property is repeatability: the same input produces the same failure on every attempt, so redelivery cannot make progress. In an unordered work queue the effect is a hot retry loop that consumes consumer capacity. In an ordered log such as a Kafka partition the effect is worse: **the partition has a single consumer position, so the poison record at the head blocks every record behind it** until the consumer either succeeds or advances past it. Throughput for that partition falls to zero.

## The core mechanic: count, then divert

Every DLQ design answers two questions: **how attempts are counted**, and **where the message goes once the count is exceeded**. Both endpoints of the design space are defects — retrying in place without bound, and dropping the message silently.

```
process(msg):
  try:
    handle(msg); ack(msg)
  except RetryableError:
    if attempts(msg) < MAX_ATTEMPTS:
      redeliver_later(msg)        # with backoff, not a hot loop
    else:
      send_to_dlq(msg, reason)    # quarantine, retaining the reason
      ack(msg)                    # removes it from the live queue
  except FatalError:
    send_to_dlq(msg, reason); ack(msg)
```

Two invariants carry the design. First, **retryable and fatal failures are classified separately**: a downstream timeout may succeed on a later attempt, whereas an un-decodable payload fails identically every time and consumes the whole attempt budget to no effect. Second, **the poison message must be acknowledged off the live queue once the copy in the DLQ is durable**. If the acknowledgement is omitted the message is redelivered and the loop is unbounded; if the acknowledgement precedes the successful publish to the DLQ, a crash in between loses the message.

The attempt counter itself must survive redelivery. A counter held in consumer memory resets whenever the consumer restarts or the message is redelivered to a different instance, which makes the attempt bound unenforceable. The counter therefore lives in **broker-maintained metadata or a message header**.

## RabbitMQ: dead-letter exchanges and a delay ring

RabbitMQ implements this at the broker. A queue declares a **dead-letter exchange (DLX)** through the `x-dead-letter-exchange` argument; the RabbitMQ documentation lists the conditions under which a message is dead-lettered — republished to that exchange — among them that it is **rejected or nacked with `requeue=false`**, that its **per-message or per-queue time-to-live (TTL) expires**, and that **the queue exceeds a length limit**. RabbitMQ records the history in an **`x-death` header**, whose entries carry a `count` of how many times the message was dead-lettered via each queue. That header serves as the durable attempt counter.

Combining the TTL condition with the DLX condition yields **delayed retries without a plugin**: the message is routed to a wait queue that has no consumer, and when its TTL expires it is dead-lettered back to the main exchange.

```python
import pika
ch = pika.BlockingConnection(pika.ConnectionParameters("localhost")).channel()

# Main queue: failures dead-letter to the retry exchange.
ch.exchange_declare("orders", "direct")
ch.exchange_declare("orders.retry", "direct")
ch.exchange_declare("orders.parked", "direct")

ch.queue_declare("orders.q", arguments={
    "x-dead-letter-exchange": "orders.retry",
})

# Retry queue: no consumer, 30s TTL, then dead-letters BACK to the main exchange.
ch.queue_declare("orders.retry.q", arguments={
    "x-message-ttl": 30_000,
    "x-dead-letter-exchange": "orders",       # loops back for another attempt
})

# Parking lot: the terminal DLQ, read by an operator or a repair job.
ch.queue_declare("orders.parked.q")
ch.queue_bind("orders.q",        "orders",        routing_key="order")
ch.queue_bind("orders.retry.q",  "orders.retry",  routing_key="order")
ch.queue_bind("orders.parked.q", "orders.parked", routing_key="order")
```

The ring is `orders.q → orders.retry.q → orders.q`, one lap per 30 seconds of TTL. Termination is not supplied by the topology — the ring has no exit — so the consumer reads `x-death` and routes to the parking lot once the count reaches the bound:

```python
def on_message(ch, method, props, body):
    deaths = (props.headers or {}).get("x-death", [])
    attempts = deaths[0]["count"] if deaths else 0
    try:
        handle(body)
        ch.basic_ack(method.delivery_tag)
    except FatalError:
        publish(ch, "orders.parked", "order", body)     # no retry for a bad payload
        ch.basic_ack(method.delivery_tag)
    except RetryableError:
        if attempts >= 5:
            publish(ch, "orders.parked", "order", body)  # bound reached -> parking lot
            ch.basic_ack(method.delivery_tag)
        else:
            ch.basic_nack(method.delivery_tag, requeue=False)  # -> retry ring, +30s
```

A single per-message TTL gives a constant delay. Exponential backoff requires **one wait queue per delay tier**, since the TTL is a property of the queue or of the individual message rather than of the retry count.

## Kafka: the DLQ is built in the consumer

The Kafka broker does not dead-letter. The log is append-only and a record cannot be removed from a partition, so quarantine is implemented by the client: on repeated failure the consumer **produces the record to a separate dead-letter topic**, carrying the original headers plus the failure reason, and then commits the offset so the partition advances. Kafka Connect provides this through the `errors.deadletterqueue.topic.name` setting; Spring for Apache Kafka provides `DeadLetterPublishingRecoverer`.

Because the dead-letter topic is an ordinary topic, a repair consumer can replay its records back to the source topic once the defect is fixed. That replay capability is the difference between quarantine and discard.

### Implementation sketch (Scala)

The classification-and-bound decision, isolated from any broker client:

```scala
enum Outcome:
  case Done
  case Retry(attempt: Int)
  case Park(reason: String)

sealed trait Failure
final case class Transient(cause: Throwable) extends Failure
final case class Fatal(cause: Throwable)     extends Failure

final case class Record(payload: Array[Byte], headers: Map[String, String])

def attemptsOf(r: Record): Int =
  r.headers.get("x-attempts").flatMap(_.toIntOption).getOrElse(0)

def decide(r: Record, maxAttempts: Int)(handle: Record => Either[Failure, Unit]): Outcome =
  handle(r) match
    case Right(_)                => Outcome.Done
    case Left(Fatal(c))          => Outcome.Park(s"fatal: ${c.getMessage}")
    case Left(Transient(c)) =>
      val next = attemptsOf(r) + 1
      // The bound is enforced against the header, not against in-memory state,
      // so a consumer restart cannot reset the count.
      if next >= maxAttempts then Outcome.Park(s"exhausted after $next: ${c.getMessage}")
      else Outcome.Retry(next)

// Ordering: Park advances past the record; a dependent successor would observe a gap.
def backoff(attempt: Int, base: Long, cap: Long): Long =
  math.min(cap, base * (1L << (attempt - 1)))
```

## Pitfalls

- **The DLQ message carries the payload but not the reason.** The exception, the source topic or queue, and the partition and offset or message identifier are lost at divert time, so the quarantined records cannot be triaged and are never replayed.
- **The message is published to the DLQ but not acknowledged on the live queue.** The broker redelivers it, the consumer diverts it again, and the DLQ fills with duplicates while the original loop continues.
- **The acknowledgement precedes the DLQ publish.** A crash between the two removes the message from the live queue without a durable copy anywhere — silent loss.
- **Fatal failures consume the retry budget.** An un-decodable payload is retried the full number of attempts, occupying consumer capacity and delaying quarantine by the entire backoff schedule.
- **The attempt counter lives in consumer memory.** Restarts and redelivery to a different instance reset it, so the bound is never reached and the retry loop is effectively unbounded.
- **DLQ depth is unmonitored.** A growing DLQ is unprocessed data; without an alert on depth the loss is discovered only when a downstream reconciliation fails.
- **Ordering is assumed to survive.** Diverting message 5 while 6, 7 and 8 proceed reorders the stream; if 6 depends on 5 the result is a correctness defect, and the alternative is to halt the partition rather than advance past the failure.
- **A single wait queue is used for exponential backoff.** The TTL belongs to the queue, so every retry tier waits the same interval unless a per-message TTL or a separate queue per tier is introduced.
