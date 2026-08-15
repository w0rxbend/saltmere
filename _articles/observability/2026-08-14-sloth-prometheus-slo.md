---
title: "Sloth: Generating Prometheus SLO Rules from a Simple Spec"
date: 2026-08-14
track: observability
summary: "Sloth compiles a dozen lines of SLO YAML into the full set of Prometheus recording rules and the multi-window multi-burn-rate alerts described in the Google SRE Workbook, replacing hand-written boilerplate."
reading_time: 6
tags: [slo, error-budget, burn-rate, prometheus, sloth, sre]
sources:
  - title: "Sloth — Prometheus SLO generator (official site)"
    url: "https://sloth.dev/"
  - title: "slok/sloth — GitHub"
    url: "https://github.com/slok/sloth"
  - title: "slok/sloth — releases"
    url: "https://github.com/slok/sloth/releases"
  - title: "Google SRE Workbook — Alerting on SLOs"
    url: "https://sre.google/workbook/alerting-on-slos/"
---

**Gist.** Implementing the Google SRE Workbook's multi-window multi-burn-rate alerting by hand costs, per service level objective (SLO), a recording rule at every time window the alerts read plus two alerting rules parameterised by a catalogue of burn-rate thresholds and window pairs (Sloth's 30-day default: 14.4 over 5m/1h, 6 over 30m/6h, 3 over 2h/1d, 1 over 6h/3d) — a transcription surface where one transposed digit silently disables a page. **Sloth** treats that rule set as compiler output: a short YAML specification declares an objective and a service level indicator (SLI) as a pair of PromQL queries, and `sloth generate` emits the complete Prometheus rules file. The cost is an added build step and a generated artefact that must never be edited in place, because the next generation overwrites it.

## The specification is the input, the rules are the output

The unit of input is an SLO: a name, an objective expressed as a percentage of good events, and an SLI. Sloth's `events` SLI form takes two PromQL expressions — an `error_query` and a `total_query` — whose ratio defines the failure fraction. **Neither query hard-codes a range**; the placeholder {% raw %}`{{.window}}`{% endraw %} stands in for the rate interval, and Sloth substitutes each window it needs when it expands the template. One query text therefore yields every windowed variant.

{% raw %}
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
{% endraw %}

That is the entire input. `objective: 99.9` sets the target; **the error budget is derived rather than declared** — `1 - 0.999 = 0.1%` of events over the rolling 30-day window. `page_alert` and `ticket_alert` name the two severity tiers and carry the labels that Alertmanager routes on; the tiers correspond to the fast-burn and slow-burn alerts of the Workbook.

Generation is a single command:

```bash
sloth generate -i ./myservice.yml -o ./myservice-rules.yml
```

The output is an ordinary Prometheus rules file, referenced from `rule_files` like any other. It contains three classes of rule:

1. **SLI recording rules.** The good/total ratio is pre-computed at each of `5m`, `30m`, `1h`, `2h`, `6h`, `1d` and `3d`. Because the alert expressions read these recorded series instead of re-evaluating the raw `rate()` over a three-day range, **the expensive long-window aggregation is paid once per evaluation interval rather than once per alert**.
2. **Metadata recording rules.** The objective, the error budget and the remaining budget are exposed as series, so a dashboard can graph budget consumption without duplicating the arithmetic that the alerts use.
3. **Multi-window multi-burn-rate alerting rules.** The `page` and `ticket` alerts, wired to the threshold and window-pair combinations above.

## The invariant behind the two-window conjunction

A burn rate is the ratio of observed error rate to the error rate the budget permits. **A burn rate of 1 exhausts the budget exactly at the end of the compliance window**; a burn rate of 14.4 exhausts a 30-day budget in roughly two days. Each burn-rate condition is a conjunction of two windows of different length, and an alert tier fires when any of its conditions holds. The page alert's first condition requires the 1-hour burn rate *and* the 5-minute burn rate to exceed 14.4; its second condition pairs 6h with 30m at a threshold of 6.

The two conditions serve opposite ends of the alert lifecycle. **The long window is the admission test**: a single bad minute cannot lift a 1-hour average above 14.4, so transient spikes never page. **The short window is the reset condition**: once the incident ends, the 5-minute term falls below threshold within minutes even though the 1-hour term stays elevated, so the alert resolves without waiting for the long window to age out. Dropping either term reintroduces one of the two classic failure modes — a single-window short alert flaps on noise, a single-window long alert stays firing long after recovery. The four threshold-and-window combinations exist because a burn rate of 14.4 and a burn rate of 1 describe genuinely different situations: the first warrants waking a human, the last warrants a ticket.

Reconstructing this by hand means writing eight window expressions and four thresholds per SLO, correctly, every time. Sloth reduces the transcription surface to one number, the objective.

## Specification formats

Sloth reads three specification flavours through the same command-line interface:

- **`prometheus/v1`** — the native format shown above.
- **Kubernetes custom resource** (`PrometheusServiceLevel`) — the same fields expressed as a custom resource. The Sloth controller reconciles it, so **rules are regenerated when the resource changes** rather than when a pipeline happens to run.
- **OpenSLO** — the vendor-neutral SLO specification. The SLO definition stays portable while Sloth supplies the Prometheus implementation.

Specifications are validated ahead of generation:

```bash
sloth validate -i ./slos/
```

Running `validate` in continuous integration catches a malformed specification at review time; without it, the first evidence of a mistake is a rules file that Prometheus rejects at load, or accepts while evaluating to nothing.

Because the generated rules are plain Prometheus, scrape configuration, storage and Alertmanager are untouched. **Sloth is a compile step, not a runtime dependency**: it runs in CI, the rules are committed, and the running system has no knowledge that a generator exists. The corollary is that a stale committed rules file and its specification can disagree indefinitely, which is why regeneration belongs in the same pipeline stage as validation.

The exercise that closes the loop is a migration diff: an existing service's hand-written alerts, its SLI restated as an `error_query`/`total_query` pair, and the output of `sloth generate` compared against the rules already in place. The diff enumerates the windows and thresholds the hand-written version omitted.

## Pitfalls

- **Editing the generated rules file.** Changes survive until the next `sloth generate`, which overwrites the file wholesale; the fix silently disappears on the next pipeline run.
- **Omitting {% raw %}`{{.window}}`{% endraw %} from the SLI queries.** A hard-coded range such as `[5m]` is not substituted, so every window's recording rule computes the same 5-minute rate and the long-window burn condition measures the wrong interval.
- **`error_query` and `total_query` with mismatched selectors.** If the error query filters on a label the total query does not (or aggregates over a different set), the ratio is not a failure fraction and the burn rate is meaningless while remaining numerically plausible.
- **A `total_query` that can evaluate to zero.** Low-traffic services divide by zero during idle periods, producing gaps rather than a healthy signal, and burn-rate conditions over a gap do not fire.
- **Treating the objective as free to raise.** Moving `objective` from 99.9 to 99.99 shrinks the error budget tenfold, so every burn-rate threshold now trips at one tenth of the previous error rate; alert volume changes even though no rule was touched by hand.
- **Regenerating without re-validating.** `sloth generate` on an input that `sloth validate` would reject can produce a rules file whose failure only appears when Prometheus loads it, after the change has merged.
- **Assuming the controller and the CLI cannot both own the same rules.** A `PrometheusServiceLevel` resource reconciled in-cluster and a committed generated file for the same SLO produce two sets of rules with the same alert names.
