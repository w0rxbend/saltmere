---
title: "p99 without the raw data: t-digest, DDSketch, and why percentiles do not average"
date: 2026-08-13
track: distributed-systems
summary: "Averaging per-host p99s is statistically meaningless — in the demonstration below it is off by 32%. Streaming sketches (t-digest, DDSketch, HDR histogram) compress millions of latencies into kilobytes, and their decisive property is mergeability: host sketches combine into the true global percentile."
reading_time: 7
tags: [percentiles, t-digest, ddsketch, latency, sketches]
sources:
  - title: "Computing Extremely Accurate Quantiles Using t-Digests — Dunning & Ertl (arXiv 2019)"
    url: "https://arxiv.org/abs/1902.04023"
  - title: "DDSketch: A Fast and Fully-Mergeable Quantile Sketch with Relative-Error Guarantees — Masson, Rim & Lee (VLDB 2019)"
    url: "https://arxiv.org/abs/1908.10693"
  - title: "Computing accurate percentiles with DDSketch (Datadog Engineering)"
    url: "https://www.datadoghq.com/blog/engineering/computing-accurate-percentiles-with-ddsketch/"
  - title: "HdrHistogram: A High Dynamic Range Histogram — Gil Tene"
    url: "https://github.com/HdrHistogram/HdrHistogram"
  - title: "Everything You Know About Latency Is Wrong (Brave New Geek)"
    url: "https://bravenewgeek.com/everything-you-know-about-latency-is-wrong/"
---

**Gist.** A percentile is a point on a distribution, and the percentile of a union of distributions is not any arithmetic combination of the parts' percentiles, so a fleet-wide p99 cannot be recovered from per-host p99 values. A **quantile sketch** — a compressed, mergeable summary of the distribution rather than a single answer — restores the composition: each host ships a sketch of a few kilobytes and any aggregator merges them and reads a valid quantile. The cost is bounded approximation error, plus the operational burden of transporting and storing a structured object instead of one floating-point gauge per series.

## Why the naive aggregation fails

The standard interview framing: 50 application programming interface (API) servers each report their own p99 latency; the service-wide p99 is requested. Averaging the reported values, weighted by request count or not, is not slightly wrong — it is answering a different question. **Once a host has collapsed its distribution to one number, the information needed to place the fleet-wide 99th percentile has been discarded**, and no correction factor recovers it. If one host serves 1% of traffic at 2 s while the rest are fast, the fleet p99 can sit near 2 s while the mean of the per-host p99s barely moves.

The remedy is to ship something richer than a number and far smaller than the raw samples.

## t-digest: adaptive bins, sharp tails

Ted Dunning's t-digest represents a distribution as a few hundred **centroids**, each a mean paired with a count. A **scale function** bounds how many samples a centroid may absorb according to its position in the distribution: centroids near the median may be fat, centroids approaching q = 0 or q = 1 must stay small. The structure therefore spends its resolution where the tails are, and occupies a few kilobytes. Two digests merge by concatenating their centroid lists and re-compressing.

The limitation is stated plainly by the construction: **t-digest's accuracy is characterised empirically rather than by a proven worst-case bound**, so adversarial input orderings can degrade it. That gap is what the next sketch closes.

## DDSketch: a guarantee that can be stated

Datadog's DDSketch (VLDB 2019) uses **exponentially spaced buckets**: a value *x* is mapped to bucket ⌈log_γ *x*⌉, with γ derived from a target **relative error** α. The paper's relation is γ = (1 + α) / (1 − α). The resulting guarantee is a bound, not an observation: for a quantile whose true value is *x*, the returned value *x̂* satisfies **|x̂ − x| ≤ α·x**. The error is on the value returned, not on the rank — DDSketch does not promise that the element it returns sits at exactly rank q.

Relative error is the appropriate currency for latency. An absolute error of 2 ms is negligible against a p50 of 200 ms and ruinous against a p99.9 of 4 ms; a relative bound scales with the magnitude being measured. Merging is **exact bucket-wise addition of counts**, since two sketches built with the same γ share an identical bucket layout, and a **bucket-collapsing rule** caps memory by folding the extreme buckets together once a configured bucket limit is reached.

The layout recurs elsewhere: [Prometheus native histograms](/articles/observability/2026-07-30-prometheus-native-histograms) implement the same exponential-bucket idea as a first-class metric type — that article covers the PromQL side. The point here is that "exponential buckets plus counts" is the mergeable latency representation regardless of branding.

## HDR histogram: the in-process workhorse

Gil Tene's HdrHistogram predates both. It fixes a value range in advance and sizes buckets to a configured number of significant digits, so **recording a sample is a small number of array-index operations with no allocation**. It is larger than a t-digest but constant-size, and two histograms configured alike merge by adding bucket counts, which makes it the standard choice inside benchmark harnesses (wrk2, JMH pipelines) and latency-sensitive services.

Tene's work also names the second classic percentile error, **coordinated omission**: a load generator that pauses while the system under test stalls stops issuing requests exactly during the slow window, silently deleting the worst samples from the record.

## Mergeability is the whole property

All three structures earn their place through one algebraic law: **merge(sketch(A), sketch(B)) ≈ sketch(A ∪ B)**, exact for DDSketch and HdrHistogram bucket counts, approximate for t-digest re-compression. That law makes percentile estimation distributive. Every host maintains a local sketch, ships it each flush interval, and any aggregator — per availability zone, per service, global — merges freely and reads a statistically valid p99. The same reasoning underlies [HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation) for distinct counts: answers do not compose, compressed distributions do.

## Demonstration

The failure and the correction, using the `ddsketch` and `numpy` Python packages:

```python
import numpy as np
from ddsketch import DDSketch

rng = np.random.default_rng(42)
all_values, per_host_p99 = [], []
merged = DDSketch(relative_accuracy=0.01)      # 1% relative error

for h in range(8):
    # each host has a different latency profile (ms)
    lat = rng.lognormal(mean=3 + 0.3 * h, sigma=0.6, size=50_000)
    sk = DDSketch(relative_accuracy=0.01)
    for v in lat:
        sk.add(v)
    merged.merge(sk)                           # bucket counts add exactly
    per_host_p99.append(np.percentile(lat, 99))
    all_values.append(lat)

truth  = np.percentile(np.concatenate(all_values), 99)
naive  = np.mean(per_host_p99)                 # the classic mistake
sketch = merged.get_quantile_value(0.99)

print(f"true p99            : {truth:8.1f} ms")
print(f"merged-sketch p99   : {sketch:8.1f} ms  ({100*(sketch-truth)/truth:+.2f}%)")
print(f"avg of per-host p99 : {naive:8.1f} ms  ({100*(naive-truth)/truth:+.2f}%)")
```

Output from this script:

```text
true p99            :    425.6 ms
merged-sketch p99   :    424.2 ms  (-0.34%)
avg of per-host p99 :    290.9 ms  (-31.64%)
```

The merged sketch lands inside its promised 1%. The averaged per-host p99s understate the tail by nearly a third — a dashboard showing 291 ms against a real tail of 426 ms, about 46% higher than reported. Any latency threshold falling between those two values is reported as met while it is being missed.

### Implementation sketch (Scala)

The load-bearing part of DDSketch is the index mapping and the fact that merging is addition on a shared bucket layout.

```scala
final class DDSketch(alpha: Double):
  private val gamma: Double = (1 + alpha) / (1 - alpha)
  private val logGamma: Double = math.log(gamma)
  private var buckets: Map[Int, Long] = Map.empty
  private var count: Long = 0L

  private def index(x: Double): Int = math.ceil(math.log(x) / logGamma).toInt

  // the bucket's representative value: every point in bucket i is within alpha of it
  private def value(i: Int): Double = 2 * math.pow(gamma, i) / (gamma + 1)

  def add(x: Double): Unit =
    require(x > 0, "the log mapping is defined for positive values only")
    val i = index(x)
    buckets = buckets.updated(i, buckets.getOrElse(i, 0L) + 1)
    count += 1

  /** Exact: two sketches with the same alpha share one bucket layout. */
  def merge(other: DDSketch): Unit =
    require(other.gamma == gamma, "sketches with different gamma cannot merge")
    other.buckets.foreach: (i, c) =>
      buckets = buckets.updated(i, buckets.getOrElse(i, 0L) + c)
    count += other.count

  def quantile(q: Double): Double =
    val rank = math.floor(q * (count - 1)).toLong
    var seen = 0L
    buckets.toSeq.sortBy(_._1)
      .find: (_, c) =>
        seen += c
        seen > rank
      .map((i, _) => value(i))
      .getOrElse(Double.NaN)
```

The bucket-collapsing rule that caps memory is omitted; without it the map grows with the dynamic range of the input.

## Pitfalls

- **Averaging or summing per-host p99 gauges.** The dashboard reports a tail far below the real one — the demonstration above understates by 31.6% — because each host discarded its distribution before export.
- **Merging sketches configured with different parameters.** DDSketch bucket indices are only comparable under the same γ; combining sketches built with different relative accuracies adds counts belonging to different value ranges.
- **Feeding DDSketch non-positive values directly.** The index is ⌈log_γ *x*⌉, which is undefined at zero and for negatives; zero and negative inputs need separate handling rather than falling through the log mapping.
- **Coordinated omission in the load generator.** Measured p99 looks healthy through a stall because the generator stopped issuing requests during the stall and never recorded the slow responses.
- **Treating t-digest's tail accuracy as a guarantee.** Its error is characterised empirically, so an adversarial or strongly ordered input stream can degrade quantiles that benchmarks showed to be tight.
- **Exceeding an HdrHistogram's configured value range.** Recordings above the highest trackable value are rejected or clipped, so a stall longer than the configured range disappears from precisely the region under investigation.
