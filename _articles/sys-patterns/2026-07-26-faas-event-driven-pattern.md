---
title: "FaaS: renting a function instead of running a server"
date: 2026-07-26
track: sys-patterns
summary: "Burns' functions-as-a-service pattern trades an always-on server for stateless code that wakes on an event — suited to glue and spiky handlers, unsuited to long-lived state and tight latency budgets."
reading_time: 6
tags: [faas, event-driven, aws-lambda, knative, serverless, cloudevents, burns]
sources:
  - title: "Designing Distributed Systems, 2nd Edition, Ch. 9 — Functions and Event-Driven Processing (Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/ch09.html"
  - title: "Designing Lambda applications — AWS Lambda Operator Guide"
    url: "https://docs.aws.amazon.com/lambda/latest/operatorguide/application-design.html"
  - title: "Configuring provisioned concurrency — AWS Lambda Developer Guide"
    url: "https://docs.aws.amazon.com/lambda/latest/dg/provisioned-concurrency.html"
  - title: "Knative Eventing overview"
    url: "https://knative.dev/docs/eventing/"
  - title: "Using Triggers and sinks — Knative"
    url: "https://knative.dev/docs/getting-started/first-trigger/"
---

**Gist.** A standing pool of workers must be sized, paid for and kept alive even when no events arrive. **Functions-as-a-service (FaaS)** removes the pool: the platform creates an execution environment when an event arrives, routes the event to a stateless handler, and is free to destroy the environment afterwards. The cost is that nothing survives between invocations — state must be externalised, and an environment created on demand pays a cold-start latency that is large enough for AWS to ship a dedicated mitigation against it.

Chapter 9 of Burns' *Designing Distributed Systems* (2nd ed.) treats FaaS as the serving-side counterpart to the batch-oriented work-queue and event-driven-batch patterns: the transport can be the same broker, but the compute on each end is ephemeral rather than a standing worker pool.

## The programming model: one event in, one response out

A FaaS function has no server to configure, no process to keep alive, and no memory of the previous invocation. The unit of deployment is a handler signature; the platform owns the lifecycle around it.

```python
# handler.py — deployed as an AWS Lambda function
import json
import boto3

s3 = boto3.client("s3")
rekognition = boto3.client("rekognition")

def handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        labels = rekognition.detect_labels(
            Image={"S3Object": {"Bucket": bucket, "Name": key}},
            MaxLabels=5,
        )
        s3.put_object(
            Bucket=bucket,
            Key=f"labels/{key}.json",
            Body=json.dumps(labels["Labels"]),
        )
    return {"statusCode": 200}
```

Binding the handler to a bucket is a matter of an S3 event notification. Whether it is written with the AWS CLI, the CDK or the console, the underlying JSON is the whole wiring:

```json
{
  "LambdaFunctionConfigurations": [
    {
      "LambdaFunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:label-images",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": { "FilterRules": [{ "Name": "suffix", "Value": ".jpg" }] }
      }
    }
  ]
}
```

Nothing in that configuration names a server, a port or a replica count. **The AWS Lambda operator guide directs designs not to depend on state persisting in the execution environment between invocations.** Any state required past the return of the handler has to live in DynamoDB, S3 or a queue; a module-level variable is a cache at best and a correctness bug at worst, because the next event may or may not land in the same environment.

Knative Eventing expresses the same model on Kubernetes with the components named rather than folded into a managed bus. An event **Source** produces CloudEvents, a **Broker** collects them, and a **Trigger** filters and forwards matching events to a **sink** — typically a Knative `Service` that scales from zero:

```yaml
apiVersion: eventing.knative.dev/v1
kind: Trigger
metadata:
  name: image-labelled-trigger
spec:
  broker: images-broker
  filter:
    attributes:
      type: com.example.image.uploaded
  subscriber:
    ref:
      apiVersion: serving.knative.dev/v1
      kind: Service
      name: label-image-fn
```

The payload is a CloudEvent, the CNCF CloudEvents envelope that Knative Eventing carries, so a function's trigger logic is not written against a vendor-specific event shape:

```json
{
  "specversion": "1.0",
  "type": "com.example.image.uploaded",
  "source": "/uploads/avatars",
  "id": "3f2c9e10-9c2b-4a1e-8b7e-3a1f6c2d9e11",
  "time": "2026-07-26T14:02:11Z",
  "datacontenttype": "application/json",
  "data": { "bucket": "avatars", "key": "user-4471.jpg" }
}
```

Under either envelope the shape of the work is identical: one event, one stateless invocation, no assumption that this instance will see a second one.

### Implementation sketch (Scala)

The load-bearing consequence of the single-invocation assumption is that every durable effect belongs behind an explicit port, and that a redelivered event must not be processed twice. The `id` attribute of the CloudEvent is the natural deduplication key, since it is carried in the envelope rather than derived from the payload.

```scala
final case class CloudEvent(
    id: String,
    `type`: String,
    source: String,
    data: String
)

/** External state. Nothing survives in the process between invocations. */
trait EffectStore:
  /** Records the id and reports whether this invocation claimed it first. */
  def claim(eventId: String): Boolean
  def put(key: String, body: String): Unit

def handler(event: CloudEvent, store: EffectStore): Unit =
  if !store.claim(event.id) then ()          // already processed; redelivery
  else
    event.`type` match
      case "com.example.image.uploaded" =>
        store.put(s"labels/${event.id}.json", label(event.data))
      case other =>
        throw IllegalArgumentException(s"unhandled event type: $other")

def label(data: String): String = ???
```

The handler is a pure function of `(event, store)`: no field of the enclosing object is read or written, so two concurrent environments running the same code cannot interfere. **`claim` must be an atomic conditional write** — a read followed by a separate write leaves a window in which two redeliveries both observe the event as unclaimed.

## Composing functions into a flow

Functions are deliberately small, so systems chain them: an upload triggers a resize function, whose "resized" event triggers a thumbnail-index function, whose event a notification function consumes. The chapter frames such systems as functions plugged into an **event flow** rather than calling one another directly. **The trigger and broker layer is the wiring**, which is what allows a function to be replaced or inserted without editing its neighbours — a caller holding a direct reference would have to be redeployed instead.

## Applicability

| Fits FaaS well | Does not fit FaaS |
|---|---|
| Event-driven glue: reacting to an S3 upload, a database change stream, a Pub/Sub message | Long-lived in-memory state (caches, sessions, models too large to reload per call) |
| Spiky, unpredictable traffic, where scale-to-zero removes cost between bursts | Steady, high-volume traffic, where per-invocation billing exceeds the cost of an always-on instance |
| Short request handlers: validate, transform, forward | Latency budgets tighter than a cold start |
| Decoupled pipeline steps, each independently deployable | Work that must run continuously rather than on a trigger |
| Fan-out processing where each unit of work is independent | Computation exceeding the platform's execution-time ceiling |

Cold starts are the sharpest edge in that table, and their significance is documented rather than anecdotal: **AWS ships provisioned concurrency, which pre-initialises execution environments, and documents it for latency-sensitive synchronous paths.** Enabling it changes the economics of the pattern — the environments are kept warm, and therefore paid for, whether or not events arrive. The chapter positions FaaS as one component of a broader architecture rather than a complete solution.

## The decision in practice

The discriminating question is what must be true between invocations. Where the answer is "nothing" — a webhook handler, a file-uploaded reaction, a scheduled cleanup — FaaS removes a server that would otherwise be operated. Where the answer names a warm cache, an open connection pool, a coordination protocol or a latency budget smaller than a cold start, a replicated long-running service is the fit (or the work-queue pattern, where the work is batchable), with functions confined to the edges where events originate.

A direct measurement settles the cold-start question for a given workload: deploy the handler above against a bucket, upload a JPEG, and compare the CloudWatch Logs gap between `START` and the first application log line on a cold invocation against a warm one, then repeat with provisioned concurrency enabled.

## Pitfalls

- **Module-level state read as a cache returns stale or missing data intermittently.** The environment is reused for some invocations and destroyed for others, so a variable populated by a previous event is present or absent nondeterministically.
- **A redelivered event applies its effect twice.** Event sources may deliver an event more than once; without an atomic claim on a stable identifier such as the CloudEvent `id`, the second delivery repeats the write.
- **A synchronous user-facing path misses its latency target under low traffic.** Idle periods let environments be reclaimed, so the next request pays a cold start precisely when traffic is thin.
- **Provisioned concurrency is enabled and the cost model silently inverts.** Pre-initialised environments are billed while idle, removing the scale-to-zero property that motivated the choice of FaaS.
- **A long computation is truncated mid-write.** Exceeding the platform's execution-time ceiling terminates the invocation after partial effects have been committed to external stores, leaving inconsistent state unless each step is separately idempotent.
- **A chain of functions has no single place to observe a request.** Because functions are wired through a broker rather than calling each other, a failure surfaces as a missing downstream event rather than an error return at the origin.
