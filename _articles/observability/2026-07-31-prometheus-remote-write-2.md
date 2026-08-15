---
title: "Prometheus Remote-Write 2.0: string interning, native histograms, and metadata in one payload"
date: 2026-07-31
track: observability
summary: "Remote-Write 1.0 repeated every label string in every series and left metric metadata behind. The 2.0 protocol adds a per-request symbols table that interns strings, carries native histograms, metadata, exemplars and created timestamps inline, and requires receivers to report what they accepted."
reading_time: 6
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

**Gist.** Remote-Write 1.0 serialises each `TimeSeries` with its labels as full strings, so label names and values that are shared across thousands of series in a batch are encoded once per series. Remote-Write 2.0 replaces the inline strings with **integer indices into a single per-request `symbols` table**, and uses the same interning to carry metric metadata, native histograms, exemplars and created timestamps in the one message. The cost is a protocol that is still marked **EXPERIMENTAL**, that requires both peers to agree on a message type through content negotiation, and that makes the sender responsible for interpreting a new class of partial-success response.

## The redundancy 1.0 encodes

The 1.0 message is `prometheus.WriteRequest`. Each `TimeSeries` inside it holds a repeated `Label` field of `(name, value)` string pairs. A batch drawn from one scrape job repeats `__name__`, `job`, `instance` and the corresponding values in **every** series, because the protobuf encoding has no back-reference mechanism: there is no way for series *n* to say "my `job` value is the same one series 1 used".

Snappy compression, which the specification mandates as the content encoding, removes much of that redundancy on the wire. It does not remove the work of building the redundant structure: the sender allocates and serialises the repeated strings, and the receiver decompresses and parses them again. The interning in 2.0 attacks the encoded form itself, so the saving appears **before compression runs** as well as after it.

## The symbols table

The 2.0 message is **`io.prometheus.write.v2.Request`**. It carries a single repeated-string field, **`symbols`**, and every string that would previously have been inlined is replaced by a `uint32` index into that table.

```text
1.0:  every series repeats "job", "api", "instance", "10.0.0.5:9090", ...
2.0:  symbols = ["", "job","api","instance","10.0.0.5:9090","__name__", ...]
      series.labels_refs = [1,2, 3,4, 5,6]   # index pairs into `symbols`
```

Labels are represented as **`labels_refs`: a flat, even-length list of indices interpreted as alternating name and value references**. Two invariants follow from that representation and are worth stating explicitly, because a hand-written sender can violate either one:

- **The list length is even.** An odd length has no meaning; there is no defaulting rule that would supply a missing value.
- **Every index is in range for the request's own `symbols` table.** The table is scoped to the single request, so indices are not stable across requests and cannot be cached by a receiver between calls.

**Index 0 is reserved for the empty string.** That gives an unambiguous encoding for an absent optional string — a help text or unit that was never set — without a separate presence flag.

The compression ratio the table achieves is a function of **label repetition within the batch**. A batch of *k* series that share a common label set of *m* strings encodes those strings once rather than *k* times; a batch of *k* series each with unique values encodes roughly the same bytes as 1.0 plus the index overhead. The mechanism therefore pays for itself on steady fleets and pays least on high-cardinality churn.

## Three payloads 1.0 left on the floor

**Native histograms, inline.** 2.0 carries Prometheus's native (sparse, exponential-bucket) histograms as a first-class field on the series. The 1.0 specification defines no field for them, so a histogram had to be flattened into classic bucket counters before it could travel over remote write.

**Metadata, per series.** Metric **type**, **help** text and **unit** are attached to each series: the type as an enum, help and unit as symbol references. Under 1.0 metadata travelled on a separate path and was routinely dropped, which left a receiver inferring counter-versus-gauge from the metric name.

**Created timestamps and exemplars.** A series may carry a **`created_timestamp`**, the start time of a cumulative series. Without it, a receiver cannot distinguish a counter's genuine first sample from a reset, which is the origin of a class of incorrect `rate()` results at the start of a series. **Exemplars** — the trace-identifier links from a sample to a representative trace — are carried inline rather than through a side channel.

## Negotiation and the written-counts response

A 2.0 sender declares the message type in the content type, not only in a version header:

```
Content-Type: application/x-protobuf;proto=io.prometheus.write.v2.Request
Content-Encoding: snappy
X-Prometheus-Remote-Write-Version: 2.0.0
```

The response carries per-type counts of what the receiver stored:

```
X-Prometheus-Remote-Write-Samples-Written:   4231
X-Prometheus-Remote-Write-Histograms-Written: 118
X-Prometheus-Remote-Write-Exemplars-Written:  20
```

This changes what a `2xx` means. Under 1.0, a success status confirmed only that the request was accepted; a receiver that discarded exemplars, or ignored a data type it did not support, reported the same `2xx` as one that stored everything. Under 2.0 the sender can compare **sent counts against written counts per type** and detect a silent partial write.

Negotiation is not automatic. A receiver that does not support the offered message type or encoding **responds `415 Unsupported Media Type`**, and the specification leaves it to the sender whether to retry with a different type. Prometheus itself selects the message type from static configuration rather than probing the endpoint, so a 2.0 endpoint pointed at a 1.0-only receiver produces failed writes rather than a transparent downgrade.

### Implementation sketch (Scala)

The interning step is the load-bearing idea: a per-request table built by a first-wins map, with labels emitted as index pairs.

```scala
final class SymbolTable:
  private val index = scala.collection.mutable.LinkedHashMap[String, Int]("" -> 0)

  /** Index 0 is reserved for the empty string, so an absent optional
    * string and a present empty one encode identically. */
  def intern(s: String): Int =
    index.getOrElseUpdate(s, index.size)

  def symbols: Vector[String] = index.keys.toVector

final case class Series(labelsRefs: Vector[Int], samples: Vector[(Long, Double)])

def encodeBatch(
    batch: Vector[(Map[String, String], Vector[(Long, Double)])]
): (Vector[String], Vector[Series]) =
  val table = SymbolTable()
  val series = batch.map: (labels, samples) =>
    // Sorted by label name so identical label sets produce identical refs.
    val refs = labels.toVector.sortBy(_._1).flatMap: (n, v) =>
      Vector(table.intern(n), table.intern(v))
    Series(refs, samples)
  // symbols must be emitted after all interning: indices are request-scoped.
  (table.symbols, series)
```

`LinkedHashMap` preserves insertion order, so `symbols(i)` is the string interned at index `i`. The decoder is the inverse and needs one bounds check per reference, since a malformed sender can emit an index past the end of the table.

## Turning it on

On Prometheus 3.x the message type is selected per remote endpoint:

```yaml
remote_write:
  - url: https://mimir.example/api/v1/push
    protobuf_message: io.prometheus.write.v2.Request   # opt into 2.0
    send_exemplars: true
```

The endpoint must be a receiver that advertises 2.0 support. Comparing `prometheus_remote_storage_bytes_total` between a 1.0 endpoint and a 2.0 endpoint fed from the same scrape targets isolates the effect of interning on that specific label distribution.

## Pitfalls

- **A `2xx` from a 1.0-era receiver still means "bytes accepted", not "everything stored".** A sender that does not read the written-counts headers cannot distinguish a full write from one that silently dropped every exemplar.
- **A receiver that does not support the offered message type answers `415`, not a downgrade.** Configuring `protobuf_message: io.prometheus.write.v2.Request` against a 1.0-only endpoint fails the write rather than falling back on its own.
- **Symbol indices are request-scoped.** A receiver that caches a table across requests, or a sender that reuses indices from a previous batch, resolves labels to the wrong strings rather than erroring.
- **An odd-length `labels_refs` has no valid interpretation.** A hand-written encoder that drops a value silently shifts every subsequent name/value boundary in that series.
- **The interning saving scales with label repetition, not with batch size.** High-cardinality churn, where few strings repeat across series, gains little over 1.0.
- **The specification is marked EXPERIMENTAL.** Pinning sender and receiver versions is what keeps a protocol-level change from arriving during an unrelated upgrade.
