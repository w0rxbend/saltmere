---
title: "Burn-Rate Alerting: Multi-Window SLO Alerts in Prometheus"
date: 2026-07-27
track: observability
summary: "Define an SLI, an SLO target, and an error budget, then build the Google SRE Workbook's multi-window multi-burn-rate alerts in Prometheus with real recording and alerting rules."
reading_time: 6
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

Once you have Prometheus 3 scraping request metrics and exemplars linking spikes back to traces (both covered elsewhere in this track), the next question is *what should actually page a human*. Alerting on "error rate > 1% for 5 minutes" is the classic trap: too tight and you page on every blip, too loose and you sleep through a real outage. The Google SRE Workbook's answer is to alert on **how fast you are spending your error budget**. Here is the recipe, with the exact numbers.

## SLI, SLO, and the error budget

An **SLI** (Service Level Indicator) is a ratio of good events to valid events. For an HTTP service the natural SLI is *successful requests / total requests*.

An **SLO** (Service Level Objective) is the target for that SLI over a rolling window — say **99.9% of requests succeed over 30 days**.

The **error budget** is the inverse: `1 - SLO`. At 99.9% you are allowed `0.1%` of requests to fail over the window. Over 30 days that budget is a fixed, spendable quantity. Spend it slowly and you are fine; spend it fast and you are heading for an SLO miss.

## Burn rate

Burn rate is how fast, relative to the SLO, the service consumes the error budget. A **burn rate of 1** means you are spending the budget at exactly the rate that exhausts it precisely at the end of the 30-day window — for a 99.9% SLO, a sustained `0.1%` error rate. Double the errors and you burn at 2, exhausting the budget in 15 days. The relationship is linear:

| Burn rate | Error rate (99.9% SLO) | Budget exhausted in |
|-----------|------------------------|---------------------|
| 1 | 0.1% | 30 days |
| 2 | 0.2% | 15 days |
| 10 | 1% | 3 days |
| 1,000 | 100% | ~43 minutes |

The insight: burn rate normalizes across SLO targets and time windows, so one alerting framework works for every service.

## Why multi-window

A single fast alert (high burn over 5 minutes) has good *detection time* but poor *precision* — a brief spike pages you needlessly, and it *resets* slowly if you require a long duration. A single slow alert has good precision but wakes you too late. The Workbook combines them: require a **long window** to establish that budget burn is significant, **and** a **short window** to confirm the problem is still happening right now. The short window sharply cuts reset time — once the incident clears, the short window drops below threshold in minutes and the alert stops firing.

These are the Workbook's recommended parameters for a 99.9% SLO (Table 5-8):

| Severity | Long window | Short window | Burn rate | Budget consumed |
|----------|-------------|--------------|-----------|-----------------|
| Page | 1 hour | 5 minutes | 14.4 | 2% |
| Page | 6 hours | 30 minutes | 6 | 5% |
| Ticket | 3 days | 6 hours | 1 | 10% |

Each row's burn rate is derived from the budget you are willing to spend before alerting. Burning 2% of a 30-day budget in one hour requires `0.02 × 720h / 1h = 14.4×` the sustainable rate. The fast page catches sudden catastrophic burns; the slower page catches grinding degradation; the ticket catches slow leaks that do not warrant a 3 a.m. wake-up.

## The Prometheus rules

First, pre-compute the error ratio at each window with recording rules so alert evaluation stays cheap. The SLI expression is just a ratio of `rate()`s:

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

Then the alert `or`s together the fast and slow window pairs. `0.001` is the error budget (`1 - 0.999`); each threshold is `burn_rate × budget`:

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

Because both sub-conditions in each pair must hold, you get the precision of the long window with the fast reset of the short one — the whole point of the multi-window design. Add the `3d`/`6h`, burn-rate-1 pair as a separate `severity: ticket` alert for slow leaks.

Tools like Sloth and Pyrra generate exactly these rule sets from a compact SLO spec, so you rarely hand-write all the windows in production — but knowing what they emit, and why the numbers are what they are, is what lets you tune them.

**Try next:** Stand up the recording rules above against a demo service, then use `curl` or a load generator to inject a burst of 500s and watch the `rate1h`/`rate5m` pair cross `14.4 × 0.001` in Prometheus's expression browser — then stop the burst and time how long the alert takes to reset.
