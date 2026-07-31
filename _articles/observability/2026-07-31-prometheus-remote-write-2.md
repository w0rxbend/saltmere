---
title: "Prometheus Remote-Write 2.0: string interning, native histograms, and metadata in one payload"
date: 2026-07-31
track: observability
summary: "Remote-Write 1.0 shipped every label string over and over and left metadata behind. The 2.0 protocol adds a per-request symbols table that interns strings, carries native histograms, metadata, exemplars and created-timestamps inline, and makes receivers report exactly what they accepted. Here's what changed and how to turn it on."
reading_time: 5
tags: [prometheus, remote-write, native-histograms, protobuf, metadata, observability]
sources:
  - title: "Prometheus Remote-Write 2.0 specification (prometheus.io/docs/specs/prw)"
    url: "https://prometheus.io/docs/specs/prw/remote_write_spec_2_0/"
  - title: "Remote-Write 1.0 specification (for comparison)"
    url: "https://prometheus.io/docs/specs/prw/remote_write_spec/"
  - title: "prometheus/prometheus #13105 — [meta] Remote write 2.0"
    url: "https://github.com/prometheus/prometheus/issues/13105"
  - title: "Prometheus 3.0 release notes / blog"
    url: "https://prometheus.io/blog/2024/11/14/prometheus-3-0/"
  - title: "Prometheus native histograms documentation"
    url: "https://prometheus.io/docs/specs/native_histograms/"
---

If you've ever run a remote-write pipeline at volume, you've seen the bandwidth bill. Remote-Write 1.0's protobuf (`prometheus.WriteRequest`) is simple and effective, but wasteful in one specific way: every `TimeSeries` carries its labels as full strings, so `__name__`, `job`, `instance`, and every other label value gets serialized *again* for every series in the batch, even though the same strings repeat thousands of times. Snappy compression helps, but you're still paying to build and compress all that redundancy. Remote-Write 2.0 attacks exactly this, and folds in three things 1.0 couldn't carry.

## The headline change: a symbols table

The new message is **`io.prometheus.write.v2.Request`**. Its core trick is **string interning**. The request holds a single repeated-string field — the **`symbols`** table — and everywhere a string used to appear, the series now stores an **integer index** into that table instead.

```text
1.0:  every series repeats "job", "api", "instance", "10.0.0.5:9090", ...
2.0:  symbols = ["", "job","api","instance","10.0.0.5:9090","__name__", ...]
      series.labels_refs = [1,2, 3,4, 5,6]   # index pairs into `symbols`
```

Labels become `labels_refs`: a flat list of integer pairs (name-index, value-index). A batch that shares labels across many series — which is essentially all of them — shrinks substantially before compression even runs. Index 0 is reserved for the empty string.

## Three payloads 1.0 left on the floor

**Native histograms, inline.** 2.0 carries Prometheus's native (sparse, exponential-bucket) histograms as a first-class field. Under 1.0 you effectively couldn't remote-write them; now the high-resolution histogram type that landed with Prometheus 3.0 travels end-to-end.

**Metadata, per series.** In 1.0, metric **type**, **help** text, and **unit** rode a separate, awkward path and were routinely dropped. 2.0 attaches metadata to each series (type as an enum, help/unit as symbol references), so a receiver finally knows a series is a counter vs a gauge without guessing from the name.

**Created timestamps and exemplars.** Each series can carry a **`created_timestamp`** (the start time for a counter/cumulative), which kills a whole class of first-scrape rate errors where a counter's true zero point was unknown. **Exemplars** — the trace-ID links that let you jump from a metric spike to an example trace — are carried inline too.

## The handshake and the receipts

Content negotiation is explicit. A 2.0 sender advertises:

```
Content-Type: application/x-protobuf;proto=io.prometheus.write.v2.Request
Content-Encoding: snappy
X-Prometheus-Remote-Write-Version: 2.0.0
```

and — this is new and genuinely useful — the **receiver must acknowledge what it actually stored** via response headers:

```
X-Prometheus-Remote-Write-Samples-Written:   4231
X-Prometheus-Remote-Write-Histograms-Written: 118
X-Prometheus-Remote-Write-Exemplars-Written:  20
```

Under 1.0 a `2xx` meant "I got the bytes," and partial drops (an unsupported histogram, a rejected exemplar) were invisible. Now the sender gets a per-type written-count, so it can detect and alert on silent partial writes instead of assuming success. Note the spec is still marked **EXPERIMENTAL**, and senders/receivers negotiate down to 1.0 when a peer doesn't speak 2.0 — so rollout is incremental, not a flag day.

## Turning it on

On a modern Prometheus (3.x) the sender is configured per remote endpoint:

```yaml
remote_write:
  - url: https://mimir.example/api/v1/push
    protobuf_message: io.prometheus.write.v2.Request   # opt into 2.0
    metadata_config:
      send: true            # 2.0 carries metadata inline
    send_exemplars: true
```

Point it at a receiver that advertises 2.0 support (recent Mimir, Thanos Receive, Cortex, or an OTel Collector build), watch the negotiated version in the sender's logs, and compare outbound bytes against your old 1.0 pipeline — the interning win shows up immediately on label-heavy workloads.

## Caveats

It's still experimental, so pin versions and test the negotiation path (mismatched peers fall back to 1.0, which you want to *observe*, not discover in an incident). The written-response headers only help if your sender actually inspects them — check that your build surfaces the counts as metrics. And the interning win scales with **label repetition**: batches of highly unique series (exemplar-heavy, high-cardinality churn) benefit less than steady fleets sharing labels.

**Try next:** stand up a Prometheus 3.x scraping a couple of targets, add two `remote_write` blocks to the same receiver — one default (1.0), one with `protobuf_message: io.prometheus.write.v2.Request` — and compare `prometheus_remote_storage_bytes_total` between them over an hour. On a label-rich target you'll see the symbols table pay for itself in raw egress before compression even enters the picture.
