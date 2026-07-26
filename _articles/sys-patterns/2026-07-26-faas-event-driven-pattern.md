---
title: "FaaS: renting a function instead of running a server"
date: 2026-07-26
track: sys-patterns
summary: "Burns' functions-as-a-service pattern trades an always-on server for stateless code that wakes up on an event — a great fit for glue and spiky handlers, a poor fit for long-lived state and tight latency."
reading_time: 5
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

The work-queue pattern scales batch computation by pointing a pool of long-lived workers at a shared queue. FaaS asks a sharper question: what if there's no pool at all? What if the compute doesn't exist until an event needs it, and disappears the moment the response is sent? Burns' Chapter 9 of *Designing Distributed Systems* (2nd ed.) calls this **functions-as-a-service** — the serving-side sibling of the batch-oriented event pipelines in his work-queue and event-driven-batch chapters, and a genuinely different trade-off, not just a smaller container.

## The programming model: stateless function, one event in, one response out

A FaaS function has no server to configure, no process to keep alive, and — critically — no memory of the last invocation. The platform decides when to create an execution environment, routes one event to it, collects the return value, and is free to destroy that environment afterward. Your code is reduced to a handler signature:

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

Wired to a bucket via an S3 event notification, the trigger configuration (set with the AWS CLI, CDK, or console) looks like this in its underlying JSON form:

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

Nothing here names a server, a port, or a replica count. Every JPEG landing in the bucket independently spins up an environment, runs the handler, and tears it down. AWS's own Lambda operator guide is blunt about the implication: "you should assume that the environment exists only for a single invocation" — any state you need past that call has to live in DynamoDB, S3, or a queue, not in a module-level variable.

Knative Eventing expresses the same model on Kubernetes with the pieces named explicitly instead of hidden inside a managed bus: an event **Source** produces CloudEvents, a **Broker** collects them, and a **Trigger** filters and forwards matching events to a **sink** — typically a Knative `Service` that scales from zero:

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

The event itself is a standard CloudEvent — the same envelope format Knative, Azure Event Grid, and dozens of other platforms converged on so that a function's trigger logic doesn't have to know which vendor emitted the event:

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

Whether the platform is Lambda's proprietary event shape or Knative's CloudEvents, the shape of the work is identical: one event, one stateless invocation, no assumption that this instance will ever see another event.

## Composing functions into a flow

Individual functions are deliberately small, so real systems chain them: an upload triggers a resize function, which emits a "resized" event that triggers a thumbnail-index function, which emits an event a notification function consumes. Burns frames this as functions plugged into an **event flow** rather than calling each other directly — the trigger/broker layer is the wiring, and any function can be replaced or added without touching its neighbors. This is where FaaS and the work-queue pattern meet: a queue or broker is often the transport between functions, but the compute on each end is ephemeral rather than a standing worker pool.

## When it fits, and when it doesn't

| Fits FaaS well | Doesn't fit FaaS |
|---|---|
| Event-driven glue: react to an S3 upload, a database change stream, a Pub/Sub message | Long-lived in-memory state (caches, sessions, ML models too big to reload per call) |
| Spiky, unpredictable traffic — scale-to-zero saves money between bursts | Steady, high-volume traffic where per-invocation billing costs more than a always-on box |
| Simple, short request handlers (validate, transform, forward) | Sub-10ms latency budgets — cold starts add hundreds of milliseconds |
| Decoupled steps in a pipeline, each independently deployable | Background jobs or coordination that must run continuously, not just on-trigger |
| Fan-out processing where each unit of work is independent | Long-running computation past the platform's execution-time ceiling |

Cold starts are the sharpest edge in that table, and they are not folklore — AWS ships a dedicated mitigation, **provisioned concurrency**, that pre-initializes execution environments specifically because on-demand starts are slow enough to violate latency SLAs for synchronous, user-facing paths. Reaching for that feature is itself an admission: past a certain latency requirement, you're paying to make FaaS behave like an always-on server, at which point Burns' advice is worth taking literally — treat FaaS "as a component in a broader architecture rather than a complete solution," not the default shape for every service.

## The decision in practice

Before reaching for a function, ask what happens between invocations. If the answer is "nothing, and that's fine" — a webhook handler, a file-uploaded reaction, a scheduled cleanup — FaaS removes a server you'd otherwise babysit. If the answer involves a warm cache, an open connection pool, a coordination protocol, or a latency budget cold starts can't meet, you want a regular replicated service (or the work-queue pattern, if the work is batchable) and a function only at the edges where events actually originate.

**Try next:** Deploy the Lambda handler above against a real S3 bucket, upload a JPEG, and watch the CloudWatch Logs timestamp gap between `START` and your first log line on a cold invocation versus a warm one — then enable provisioned concurrency and measure the difference directly.
