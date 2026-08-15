---
title: "Alertmanager Beyond the Default Route: Trees, Grouping, Inhibition, and Silences"
date: 2026-08-15
track: observability
summary: "The default Alertmanager config dumps every alert into one receiver, and the three timing knobs — group_wait, group_interval, repeat_interval — are the most misunderstood settings in the Prometheus stack. Here's how the routing tree actually walks, how inhibition kills the notification storm when a node dies, and when to use silences versus time intervals. Current as of Alertmanager 0.33." 
reading_time: 5
tags: [alertmanager, prometheus, alerting, routing, inhibition, on-call]
sources:
  - title: "Alertmanager — Configuration reference (route, inhibit_rules, time_intervals)"
    url: "https://prometheus.io/docs/alerting/latest/configuration/"
  - title: "Alertmanager — Overview (grouping, inhibition, silences, HA)"
    url: "https://prometheus.io/docs/alerting/latest/alertmanager/"
  - title: "Alertmanager v0.33.1 release notes"
    url: "https://github.com/prometheus/alertmanager/releases/tag/v0.33.1"
  - title: "What's the difference between group_interval, group_wait, and repeat_interval? (Robust Perception)"
    url: "https://www.robustperception.io/whats-the-difference-between-group_interval-group_wait-and-repeat_interval/"
  - title: "Alertmanager — High Availability (README)"
    url: "https://github.com/prometheus/alertmanager#high-availability"
---

Your burn-rate alerts are precise, multiwindow, severity-labeled — and they all land in the same Slack channel as `KubeletTooManyPods`, because nobody ever edited the routing tree. Alertmanager (currently **0.33.1**, July 2026) is where alert *quality* becomes notification *quality*, and almost all of its power lives in four config blocks: the route tree, the grouping timers, inhibition rules, and time intervals.

## The routing tree walks depth-first, first match wins

The `route` block is a tree. Every alert enters at the root (which must match everything — never put matchers on it) and walks children **depth-first**. The first child whose **matchers** all pass wins; the alert descends into that child and its sub-routes, inheriting any field the child doesn't override. Siblings after the match are never evaluated — unless the matching route sets **`continue: true`**, in which case the walk resumes with the next sibling. That's the fan-out mechanism: an audit route with `continue: true` can copy every critical alert to a webhook *and* let it proceed to the team route that actually pages someone.

Two things bite people: children inherit `group_by`, timers, and receiver from their parent, so an override three levels up silently applies below; and matcher order is routing policy — put specific routes before general ones.

## What the three timers actually control

These are per-*group*, not per-alert. A group is the set of firing alerts that share values for the labels in **`group_by`** (e.g. `[alertname, cluster, service]`; the special value `[...]` disables grouping entirely).

| Timer | Default | What it really controls |
|-------|---------|-------------------------|
| `group_wait` | 30s | How long to sit on a *brand-new* group before the first notification — the buffer that turns 40 near-simultaneous alerts into one page |
| `group_interval` | 5m | Minimum wait before notifying again about a group whose *contents changed* (new alert joined, or one resolved) |
| `repeat_interval` | 4h | How long before re-sending a notification for a group that *hasn't changed at all* — the "are you still ignoring this?" nag |

So: `group_wait` is paid once per group, `group_interval` gates change notifications, and `repeat_interval` gates pure repeats (it's effectively rounded up to a multiple of `group_interval`, since repeats are only evaluated on group ticks). Short `group_wait` (10s) for pages where seconds matter; long `repeat_interval` (24h+) for ticket-queue receivers so the queue isn't spammed.

## Inhibition: let the root cause mute its symptoms

An **inhibition rule** suppresses target alerts while a source alert fires — the classic being "node down mutes everything on that node." The **`equal`** list is the join key: source and target must carry identical values for those labels, otherwise one dead node would mute the whole fleet.

Inhibition is evaluated cluster-wide and continuously; when the source resolves, the targets un-mute on their own. It's the right tool for *causal* relationships you know at config time. One caveat: make sure the source alert can't inhibit itself into oblivion via a chain (a target of one rule being the source of another is legal and occasionally surprising).

## Silences vs. time intervals

**Silences** are ad-hoc and data-driven: created in the UI or via `amtool silence add`, they're matcher-based, have an expiry, and are replicated across the HA cluster. Use them for "we're migrating this database tonight."

**Time intervals** are config-driven and recurring: named schedules in the top-level `time_intervals` block (the old top-level `mute_time_intervals` section is deprecated), referenced from routes via `mute_time_intervals` (route goes quiet during the window) or `active_time_intervals` (route only fires during the window). That's how you express "warnings go to Slack only during business hours" without anyone having to remember to re-create a silence every Friday.

## One realistic config

```yaml
route:
  receiver: default-slack            # root: no matchers, catches strays
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity = critical]
      receiver: audit-webhook
      continue: true                 # log every page, then keep routing
    - matchers: [team = payments]
      receiver: payments-slack
      routes:
        - matchers: [severity = critical]
          receiver: payments-pagerduty
          group_wait: 10s
          repeat_interval: 1h
        - matchers: [severity = warning]
          receiver: payments-slack
          active_time_intervals: [business-hours]
    - matchers: [alertname = Watchdog]
      receiver: deadmans-switch
      repeat_interval: 5m            # heartbeat: absence = Prometheus is down

inhibit_rules:
  - source_matchers: [alertname = NodeDown]
    target_matchers: [severity =~ "warning|info"]
    equal: [cluster, instance]       # only mute alerts on *that* node

time_intervals:
  - name: business-hours
    time_intervals:
      - weekdays: ["monday:friday"]
        times: [{ start_time: "09:00", end_time: "18:00" }]
        location: "Europe/Warsaw"

receivers:
  - name: default-slack
  - name: payments-slack
  - name: payments-pagerduty
  - name: audit-webhook
  - name: deadmans-switch
```

## High availability: gossip, not load balancing

Run Alertmanager as a cluster with `--cluster.peer=am-0:9094 --cluster.peer=am-1:9094 ...` and the instances form a **gossip mesh** (memberlist protocol) that replicates silences and the notification log. Crucially, you do **not** load-balance in front of it — every Prometheus sends every alert to **all** Alertmanagers. Deduplication happens on the way out: peers stagger their dispatch by cluster position, and a peer that sees (via gossip) that the notification already went out suppresses its own copy. Worst case during a partition is a duplicate page — the design deliberately prefers double-notify over never-notify.

This article pairs with the earlier SLO burn-rate piece: burn-rate rules produce well-labeled `severity: critical|warning` alerts, and the tree above is where those labels start earning their keep.

Try next: add a `Watchdog` dead-man's-switch route pointing at an external heartbeat service (Healthchecks.io or PagerDuty's dead-man integration) — it's the only alert that tests the path *from* Prometheus *through* Alertmanager while everything is healthy.
