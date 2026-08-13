---
title: "p99 without the raw data: t-digest, DDSketch, and why you can't average percentiles"
date: 2026-08-13
track: distributed-systems
summary: "Averaging per-host p99s is statistically meaningless — in the demo below it's off by 32%. Streaming sketches (t-digest, DDSketch, HDR histogram) compress millions of latencies into kilobytes, and their killer property is mergeability: combine host sketches and read the true global percentile."
reading_time: 5
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

Classic interview trap: "Each of your 50 API servers reports its own p99 latency. How do you get the service-wide p99?" The tempting answer — average them, maybe weighted by request count — is wrong, and not slightly wrong. A percentile is a point on a distribution; the percentile of a union of distributions is not any arithmetic combination of the parts' percentiles. If one host serves 1% of traffic at 2 s and the rest are fast, the fleet p99 can sit near 2 s while the *average* of per-host p99s barely moves. There is no correction factor: once each host has collapsed its distribution to a single number, the information needed is gone.

The fix is to ship something richer than a number but far smaller than the raw data: a **quantile sketch**.

## t-digest: adaptive bins, sharp tails

Ted Dunning's t-digest represents a distribution as a few hundred centroids (mean + count). A *scale function* limits how many samples a centroid may absorb depending on where it sits: centroids near the median can be fat, centroids near q=0 or q=1 must stay tiny. The result is a structure of a few KB whose accuracy is best exactly where you care — the extreme tails — with p99.9 typically resolved to a fraction of a percent. Two digests merge by combining their centroid lists and re-compressing. Caveat worth knowing: t-digest's error bound is empirical, not proven — adversarial orderings can degrade it, which is precisely the gap the next sketch was built to close.

## DDSketch: a guarantee you can state

Datadog's DDSketch (VLDB 2019) is almost embarrassingly simple: exponentially-spaced buckets, where value *x* lands in bucket ⌈log_γ x⌉ with γ chosen from your target **relative error** α. That construction gives a provable guarantee: any returned quantile q̂ satisfies |q̂ − q| ≤ α·q. Relative error is the right currency for latency — being off by 2 ms is fine at p50=200 ms and disastrous at p99.9=4 ms — and merging two DDSketches is exact bucket-wise addition, with a bucket-collapsing rule to cap memory. This bucket layout should sound familiar: [Prometheus native histograms](/articles/observability/2026-07-30-prometheus-native-histograms) are the same exponential-bucket idea as a first-class metric type — see that article for the PromQL side; the point here is that "exponential buckets + counts" is *the* mergeable latency representation, whatever the branding.

## HDR histogram: the in-process workhorse

Gil Tene's HdrHistogram predates both: fixed value range, buckets sized to a configured number of significant digits, recording is a couple of array-index operations with no allocation. It's bigger than a t-digest but constant-size, brutally fast, and losslessly mergeable — the standard choice inside benchmark harnesses (wrk2, JMH pipelines) and latency-critical services. Tene's talks also supply the other classic percentile sin, *coordinated omission*: pausing your load generator while the system stalls silently deletes the worst samples.

## Mergeability is the whole game

All three earn their place through one algebraic property: **merge(sketch(A), sketch(B)) ≈ sketch(A ∪ B)**. That makes percentile estimation *distributive*: every host keeps a local sketch, ships it each flush interval, and any aggregator — per-AZ, per-service, global — merges freely and reads a statistically valid p99. Same trick as [HyperLogLog](/articles/distributed-systems/2026-08-10-hyperloglog-cardinality-estimation) for distinct counts: don't ship answers, ship compressed distributions, because answers don't compose and distributions do.

Here's the failure and the fix in 30 lines (`pip install ddsketch numpy`):

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
    merged.merge(sk)                           # sketches merge losslessly
    per_host_p99.append(np.percentile(lat, 99))
    all_values.append(lat)

truth  = np.percentile(np.concatenate(all_values), 99)
naive  = np.mean(per_host_p99)                 # the classic mistake
sketch = merged.get_quantile_value(0.99)

print(f"true p99            : {truth:8.1f} ms")
print(f"merged-sketch p99   : {sketch:8.1f} ms  ({100*(sketch-truth)/truth:+.2f}%)")
print(f"avg of per-host p99 : {naive:8.1f} ms  ({100*(naive-truth)/truth:+.2f}%)")
```

Output from this exact script:

```text
true p99            :    425.6 ms
merged-sketch p99   :    424.2 ms  (-0.34%)
avg of per-host p99 :    290.9 ms  (-31.64%)
```

The merged sketch lands within its promised 1%; the averaged per-host p99s understate the tail by nearly a third — a dashboard that would swear an SLO was met while a chunk of users waited twice as long.

The interview summary: never aggregate percentiles, aggregate *distributions*; pick DDSketch/native histograms when you need a stated relative-error guarantee and cross-host merging, t-digest when you want tiny sketches with excellent tail behavior, HDR histogram when you control the process and want raw speed; and quote memory honestly — kilobytes per (host, endpoint) series versus gigabytes of raw samples.

**Try next:** take one latency metric you currently export as a pre-computed p99 gauge and re-export it as a distribution (DDSketch, or a Prometheus native histogram) — then compare the merged fleet p99 against the old averaged gauge during your next deploy.
