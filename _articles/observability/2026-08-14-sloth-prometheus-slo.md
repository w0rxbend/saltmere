---
title: "Sloth: Generating Prometheus SLO Rules from a Simple Spec"
date: 2026-08-14
track: observability
summary: "Sloth turns a dozen lines of SLO YAML into the full set of Prometheus recording rules and multi-window multi-burn-rate alerts from the Google SRE Workbook, so you stop hand-writing the boilerplate."
reading_time: 5
tags: [slo, error-budget, burn-rate, prometheus, sloth, sre]
sources:
  - title: "Sloth — Prometheus SLO generator (official site)"
    url: "https://sloth.dev/"
  - title: "slok/sloth — GitHub"
    url: "https://github.com/slok/sloth"
  - title: "Sloth releases (v0.16.0, 2026-04-04)"
    url: "https://github.com/slok/sloth/releases"
  - title: "Google SRE Workbook — Alerting on SLOs"
    url: "https://sre.google/workbook/alerting-on-slos/"
---

Elsewhere in this track we built the Google SRE Workbook's multi-window multi-burn-rate alerts in Prometheus by hand. That exercise is worth doing once, because it teaches you what the numbers mean. It is miserable to do twice. Each SLO needs recording rules at five or six time windows, a fast-burn page alert, a slow-burn ticket alert, and the exact burn-rate thresholds (14.4, 6, 3, 1) paired with the exact windows (5m/1h, 30m/6h, 2h/1d, 6h/3d). Copy-paste that across twenty services and one transposed digit silently breaks a page.

**Sloth** removes the boilerplate. You describe an SLO as a short YAML spec — an objective, and an SLI expressed as good/total (or error/total) event queries — and `sloth generate` emits the complete Prometheus rule set. The current release is **v0.16.0** (April 4, 2026).

## The spec is the whole point

Here is a complete availability SLO for an HTTP service. The SLI is defined by two PromQL queries; Sloth fills in `{{.window}}` for each burn window it needs.

```yaml
version: "prometheus/v1"
service: "myservice"
labels:
  owner: "myteam"
slos:
  - name: "requests-availability"
    objective: 99.9
    description: "Availability SLO for HTTP responses."
    sli:
      events:
        error_query: sum(rate(http_request_duration_seconds_count{job="myservice",code=~"(5..|429)"}[{{.window}}]))
        total_query: sum(rate(http_request_duration_seconds_count{job="myservice"}[{{.window}}]))
    alerting:
      name: MyServiceHighErrorRate
      page_alert:
        labels: {severity: page}
      ticket_alert:
        labels: {severity: ticket}
```

That is the entire input. `objective: 99.9` sets the target; the error budget (`1 - 0.999 = 0.1%` over the rolling 30-day window) is derived. `page_alert` and `ticket_alert` map to the fast-burn and slow-burn tiers.

Generate the rules:

```bash
sloth generate -i ./myservice.yml -o ./myservice-rules.yml
```

The output is a normal Prometheus rules file you drop into `rule_files`. It contains three things:

1. **SLI recording rules** — the good/total ratio pre-computed at each window (`5m`, `30m`, `1h`, `2h`, `6h`, `1d`, `3d`), so alert expressions stay cheap.
2. **Metadata recording rules** — the objective, the error budget, and the remaining budget as series you can graph directly on a dashboard.
3. **Multi-window multi-burn-rate alerting rules** — the Workbook's `page` and `ticket` alerts, wired with the correct burn-rate thresholds and window pairs. Both a short and a long window must fire together, which is what kills flapping.

## Why the two-window trick matters

A burn rate of 1 means you are spending budget exactly fast enough to exhaust it at the end of the window; a burn rate of 14.4 means you would burn a 30-day budget in about two days. Sloth's generated page alert fires only when the 1-hour *and* 5-minute burn rates both exceed 14.4 — the long window confirms a real problem, the short window makes recovery detection fast. You get this for free instead of transcribing it.

## Formats and OpenSLO

Sloth reads three spec flavors from the same CLI:

- **`prometheus/v1`** — the native format above.
- **Kubernetes CRD** (`PrometheusServiceLevel`) — same fields as a custom resource, reconciled by the Sloth controller so rules regenerate on change.
- **OpenSLO** — the vendor-neutral SLO standard. Write portable OpenSLO specs and let Sloth handle the Prometheus implementation, which keeps your SLO definitions decoupled from the tool that renders them.

Validate specs in CI before they ship:

```bash
sloth validate -i ./slos/
```

Because the generated rules are plain Prometheus, nothing about your scrape, storage, or Alertmanager setup changes. Sloth is a compile step, not a runtime dependency — it runs in CI, commits the rules, and gets out of the way.

**Try next:** Take one service you already alert on, write its SLI as an `error_query`/`total_query` pair, run `sloth generate`, and diff the output against your hand-written rules — the burn-rate thresholds you were missing will be obvious.
