---
title: "Dead Letter Queues: stop one poison message from wedging your consumer"
date: 2026-07-30
track: microservices
summary: "A single un-processable message can retry forever and block the whole partition behind it. A dead letter queue quarantines it after N attempts so the rest of the stream keeps flowing. Here's the retry-count mechanic, a RabbitMQ DLX + delayed-retry topology, and the sharp edges that catch people."
reading_time: 5
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

Message brokers give you at-least-once delivery, which is great until one message can *never* be processed — a malformed payload, a schema the consumer can't decode (see the schema-evolution article here), a referenced record that no longer exists. The naive loop retries it, fails, retries it, fails, forever. Worse, in an ordered log like a Kafka partition, that one message sits at the head and **blocks every message behind it**. This is a **poison message**, and the containment pattern is a **dead letter queue** (DLQ): after N failed attempts, move the message aside so the stream keeps moving and a human (or a repair job) can look later.

## The core mechanic: count, then divert

Every DLQ design is the same two decisions: *how do you count attempts*, and *where does the message go when the count is exceeded*. The trap to avoid is retrying **in place forever** or, equally bad, dropping the message silently. You want:

```
process(msg):
  try:
    handle(msg); ack(msg)
  except RetryableError:
    if attempts(msg) < MAX_ATTEMPTS:
      redeliver_later(msg)        # with backoff, not a hot loop
    else:
      send_to_dlq(msg, reason)    # quarantine + keep the reason
      ack(msg)                    # <-- critical: remove from the live queue
  except FatalError:
    send_to_dlq(msg, reason); ack(msg)   # no point retrying a bad payload
```

Two things people get wrong here. First, **distinguish retryable from fatal**: a downstream timeout is worth retrying; an un-decodable payload will fail identically every time, so send it straight to the DLQ and don't waste attempts. Second, **you must ack the poison message off the live queue** once it's in the DLQ — otherwise it's still there and you've built an infinite loop with extra steps.

## RabbitMQ: dead-letter exchanges and a delay ring

RabbitMQ has this built in. A queue can declare a **dead-letter exchange (DLX)**; a message is dead-lettered — republished to that exchange — when any of three things happen: it's **rejected/nacked with `requeue=false`**, its **per-message or per-queue TTL expires**, or the queue hits a **length limit**. RabbitMQ stamps an `x-death` header on the message recording how many times and via which queues it's been dead-lettered — that's your attempt counter, for free.

The elegant trick is to combine DLX with TTL to get **delayed retries** without a plugin. Bounce the message through a wait queue that has no consumer and a TTL; when the TTL expires it dead-letters *back* to the main exchange:

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

# Parking lot: the terminal DLQ. A human or repair job reads this.
ch.queue_declare("orders.parked.q")
ch.queue_bind("orders.q",        "orders",        routing_key="order")
ch.queue_bind("orders.retry.q",  "orders.retry",  routing_key="order")
ch.queue_bind("orders.parked.q", "orders.parked", routing_key="order")
```

In the consumer, read `x-death` to decide when to stop looping and park it for good:

```python
def on_message(ch, method, props, body):
    deaths = (props.headers or {}).get("x-death", [])
    attempts = deaths[0]["count"] if deaths else 0
    try:
        handle(body)
        ch.basic_ack(method.delivery_tag)
    except FatalError:
        publish(ch, "orders.parked", "order", body)     # never retry a bad payload
        ch.basic_ack(method.delivery_tag)
    except RetryableError:
        if attempts >= 5:
            publish(ch, "orders.parked", "order", body)  # give up -> parking lot
            ch.basic_ack(method.delivery_tag)
        else:
            ch.basic_nack(method.delivery_tag, requeue=False)  # -> retry ring, +30s
```

## Kafka has no native DLQ — mostly

Kafka the broker doesn't dead-letter for you (the log is immutable; you can't pull a message out of a partition). You build it in the consumer: on repeated failure, **produce the record to a separate `*.DLT` topic** with the original headers plus the failure reason and stack, then commit the offset so the partition advances. Kafka Connect and Spring Kafka ship this out of the box (`errors.deadletterqueue.topic.name` in Connect; `DeadLetterPublishingRecoverer` in Spring). Because the DLT is just another topic, a repair consumer can replay it back to the source topic after you fix the bug — the whole point of quarantining rather than dropping.

## The sharp edges

Preserve **enough context to act**: original topic/queue, partition/offset or message id, the exception, and the raw bytes — a DLQ full of payloads with no reason is a graveyard, not a tool. **Alert on DLQ depth**, because a silent, growing DLQ is data loss you haven't noticed yet. And watch **ordering**: diverting message 5 to a DLQ while 6, 7, 8 proceed means you've reordered the stream — fine for independent events, a correctness bug if 6 depended on 5. When order matters, you may have to halt the partition instead of skipping ahead.

**Try next:** Build the RabbitMQ topology above locally and publish a message your handler always rejects with `RetryableError`. Watch it loop main → retry (30s) → main five times, then land in `orders.parked.q`. Then publish one that raises `FatalError` and confirm it parks on the *first* failure — proving your retryable/fatal split actually saves the wasted attempts.
