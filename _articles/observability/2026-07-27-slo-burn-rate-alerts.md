---
title: "Burn-Rate Alerting: Multi-Window SLO Alerts in Prometheus"
date: 2026-07-27
track: observability
summary: "Define an SLI, an SLO target, and an error budget, then build the Google SRE Workbook's multi-window multi-burn-rate alerts in Prometheus with real recording and alerting rules."
reading_time: 7
tags: [slo, error-budget, burn-rate, prometheus, alerting, sre]
sources:
  - title: "Google SRE Workbook — Alerting on SLOs"
    url: "https://sre.google/workbook/alerting-on-slos/"
  - title: "How to implement multi-window, multi-burn-rate alerts with Grafana Cloud"
    url: "https://grafana.com/blog/how-to-implement-multi-window-multi-burn-rate-alerts-with-grafana-cloud/"
  - title: "Burn rate is a better error rate — Datadog"
    url: "https://www.datadoghq.com/blog/burn-rate-is-better-error-rate/"
  - title: "Prometheus — Alerting Rules"
    url: "https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/"
---

**Gist.** A fixed-threshold alert ("error rate above 1% for 5 minutes") has no principled setting: tight thresholds page on transients, loose ones miss slow degradations that still exhaust the reliability target. Alerting on *burn rate* — the ratio between the observed error rate and the error rate that would exactly consume the error budget over the objective window — normalises the threshold across services and windows, and pairing a long window with a short one buys both precision and fast reset. The cost is combinatorial: each severity requires two evaluated windows, the long windows demand recording rules and multi-day retention, and every window is a ratio that becomes undefined when traffic goes to zero.

## SLI, SLO, and the error budget

A **service level indicator (SLI)** is the ratio of good events to valid events. For a hypertext transfer protocol (HTTP) service the natural SLI is *non-5xx responses / total responses*.

A **service level objective (SLO)** is the target value of that SLI over a rolling window — for example **99.9% of requests succeed over 30 days**.

The **error budget** is the complement, `1 − SLO`. At 99.9% over a 30-day period of `T = 720` hours, the budget is `0.001` of all valid events. The budget is a quantity, not a rate: it may be spent at any tempo, and the only question an alert must answer is whether the current tempo will exhaust it before the period ends.

## Burn rate, derived

Let `e(t)` be the instantaneous error ratio and `β = 1 − SLO` the budget. Define the **burn rate** `b = e(t) / β`. A sustained `b = 1` consumes exactly the whole budget in `T`; a sustained `b` consumes it in `T / b`. The fraction of budget consumed by a burn of rate `b` held for a window of length `w` is

    f = b · w / T

That single identity generates every number in the Workbook's tables.

| Burn rate | Error rate (99.9% SLO) | Budget exhausted in |
|-----------|------------------------|---------------------|
| 1 | 0.1% | 30 days |
| 2 | 0.2% | 15 days |
| 10 | 1% | 3 days |
| 1,000 | 100% | ~43 minutes |

Because the definition divides out `β`, **the same threshold expresses the same operational urgency at 99.9% and at 99.99%** — only the absolute error ratio behind it changes. One alerting framework therefore covers services with different targets.

## Why two windows

Chapter 5 of the Google SRE Workbook, *Alerting on SLOs*, evaluates alert designs on four axes: **precision** (fraction of alerts that correspond to real budget-relevant events), **recall** (fraction of such events detected), **detection time**, and **reset time**. A single short window with a high threshold detects fast but fires on transients that consume a negligible slice of budget. A single long window has high precision but detects late and, worse, **resets late**: after an incident ends, a 1-hour average error ratio stays elevated for up to an hour, so the alert continues to fire against a healthy service and trains responders to ignore it.

The Workbook's fix is a conjunction. The long window establishes that the burn is *budget-relevant*; the short window — conventionally **one twelfth of the long window** — establishes that it is *still occurring*. Reset time falls to roughly the short window's length, because the conjunct that clears first ends the alert.

These are the recommended parameters for a 30-day 99.9% SLO, from the Workbook's recommended-parameters table:

| Severity | Long window | Short window | Burn rate | Budget consumed |
|----------|-------------|--------------|-----------|-----------------|
| Page | 1 hour | 5 minutes | 14.4 | 2% |
| Page | 6 hours | 30 minutes | 6 | 5% |
| Ticket | 3 days | 6 hours | 1 | 10% |

Each burn rate follows from `f = b · w / T` solved for `b`. Spending 2% of the budget within one hour requires `b = 0.02 × 720 / 1 = 14.4`; 5% within six hours requires `b = 0.05 × 720 / 6 = 6`; 10% within three days requires `b = 0.10 × 720 / 72 = 1`. **Detection time for a total outage (`e = 1`, hence `b = 1000` at 99.9%) under the first row is `0.02 × 720 h / 1000 ≈ 52 s`** plus the short window's own smoothing lag, while the ticket row will not fire for hours — which is the intent, since a 0.1% excess does not warrant a 03:00 page.

## The Prometheus rules

Alert expressions must stay cheap, because Prometheus re-evaluates every rule group on each interval. A `rate(...[3d])` over a high-cardinality counter scans three days of samples per evaluation; precomputing it with a recording rule reduces the alert to a comparison against a single-series lookup.

```yaml
groups:
- name: slo:http_requests
  rules:
  - record: job:slo_errors_per_request:ratio_rate5m
    expr: |
      sum(rate(http_requests_total{code=~"5.."}[5m])) by (job)
        /
      sum(rate(http_requests_total[5m])) by (job)
  # repeat for 30m, 1h, 6h, 3d windows
```

The alert disjoins the severity rows and conjoins the window pair within each row. The literal `0.001` is the budget `β`; each threshold is `b · β`.

{% raw %}
```yaml
- alert: ErrorBudgetBurn
  expr: |
    (
      job:slo_errors_per_request:ratio_rate1h{job="api"}  > (14.4 * 0.001)
        and
      job:slo_errors_per_request:ratio_rate5m{job="api"}  > (14.4 * 0.001)
    )
    or
    (
      job:slo_errors_per_request:ratio_rate6h{job="api"}  > (6 * 0.001)
        and
      job:slo_errors_per_request:ratio_rate30m{job="api"} > (6 * 0.001)
    )
  labels:
    severity: page
  annotations:
    summary: "High error-budget burn on {{ $labels.job }}"
```
{% endraw %}

The `and` in the Prometheus query language is a set intersection on label sets, not a Boolean scalar operator: it yields the left-hand samples whose label set also appears on the right. Both sides must therefore carry identical labels after aggregation, which is why each recording rule aggregates `by (job)` and nothing else. The `3d`/`6h` pair at burn rate 1 belongs in a separate rule with `severity: ticket`.

Generators such as Sloth and Pyrra emit exactly this rule shape from a compact SLO specification, so the full window matrix is rarely hand-written; the derivation above is what makes the emitted constants auditable.

### Implementation sketch (Scala)

The evaluator underneath the Prometheus expression is a fixed-size ring of per-bucket counters, from which any window's ratio is a suffix sum. Windows are aligned to a common bucket so that the long and short ratios are read from the same snapshot.

```scala
final case class Bucket(good: Long, bad: Long)

/** Ring of 1-minute buckets; capacity must exceed the longest window. */
final class BurnMeter(capacity: Int):
  private val ring = Array.fill(capacity)(Bucket(0, 0))
  private var minute = 0L

  def observe(now: Long, ok: Boolean): Unit =
    if now >= minute then                       // out-of-order minutes are dropped
      if now > minute then                      // roll: clear skipped buckets
        // a gap wider than the ring clears it once, not once per skipped minute
        var m = math.max(minute + 1, now - capacity + 1)
        while m <= now do { ring((m % capacity).toInt) = Bucket(0, 0); m += 1 }
        minute = now
      val i = (now % capacity).toInt
      val b = ring(i)
      ring(i) = if ok then b.copy(good = b.good + 1) else b.copy(bad = b.bad + 1)

  /** None when the window holds no valid events: 0/0 is undefined, not healthy. */
  def ratio(windowMinutes: Int): Option[Double] =
    val (bad, total) = (0 until windowMinutes).foldLeft((0L, 0L)):
      case ((bd, tot), k) =>
        val b = ring(((minute - k + capacity) % capacity).toInt)
        (bd + b.bad, tot + b.good + b.bad)
    Option.when(total > 0)(bad.toDouble / total)

final case class Rule(longW: Int, shortW: Int, burn: Double)

def firing(m: BurnMeter, budget: Double, rules: Seq[Rule]): Boolean =
  rules.exists: r =>
    val t = r.burn * budget
    (m.ratio(r.longW), m.ratio(r.shortW)) match
      case (Some(l), Some(s)) => l > t && s > t
      case _                  => false
```

The ring makes the cost of one evaluation `O(w)` bucket reads for window `w`, or `O(1)` amortised if a running suffix sum is maintained instead; the `None` case encodes the zero-traffic invariant that the Prometheus rules leave implicit.

## Pitfalls

- **A ratio over a window with no traffic produces no sample.** In Prometheus, `0 / 0` is `NaN` and the comparison drops the series, so a service that has stopped serving entirely — the worst outage — silently fails to satisfy the alert expression. Pair burn-rate alerts with a separate absence or low-traffic alert.
- **Averaging short-window ratios does not reconstruct the long-window ratio.** The 3-day error ratio equals the traffic-weighted mean of its constituent ratios; an unweighted mean over 5-minute ratios overweights low-traffic minutes and inflates the apparent burn overnight.
- **`rate()` extrapolates at series boundaries.** For counters that appear, reset, or vanish inside the window, `rate()` extrapolates to the window edges and can report a value the raw samples do not support — visible as brief burn spikes immediately after a deployment rolls new pods.
- **Adding `for:` to a multi-window alert double-counts the delay.** The long window already supplies persistence; a `for: 15m` on top adds latency without adding precision and can push a 52-second detection to a quarter hour.
- **The short window must divide the long one, and the recording interval must divide the short one.** A 5-minute window evaluated on a 5-minute rule interval still consumes every scrape inside the range, but consecutive evaluations no longer overlap: the ratio is refreshed once per window rather than rolling, so detection and reset are quantised to the full window length and the short window stops being short in practice.
- **Deleting or renaming a recording rule silently disables the alert.** A missing series makes the `and` intersection empty, so the alert evaluates to no samples and never fires; only rule-level unit tests (`promtool test rules`) catch this.
- **Budget arithmetic assumes a rolling window, but many dashboards render a calendar month.** A burn that exhausts the budget on the 29th resets on the 1st under a calendar view while the rolling alert continues to fire, producing contradictory reports during the same incident.
