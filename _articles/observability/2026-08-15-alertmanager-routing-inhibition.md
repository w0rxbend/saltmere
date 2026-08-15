---
title: "Alertmanager Beyond the Default Route: Trees, Grouping, Inhibition, and Silences"
date: 2026-08-15
track: observability
summary: "The default Alertmanager configuration delivers every alert to a single receiver, and the three timing parameters — group_wait, group_interval, repeat_interval — govern per-group notification behaviour rather than per-alert. This article walks the routing tree, the grouping state machine, the inhibition join key, and the difference between silences and time intervals. Current as of Alertmanager 0.33.1."
reading_time: 6
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

**Gist.** Prometheus decides *when* a condition is true; Alertmanager decides *who hears about it and how often*, and an unedited configuration collapses that decision into one receiver for every alert. Four configuration blocks carry the behaviour: the `route` tree (a depth-first, first-match-wins dispatch), the grouping timers, `inhibit_rules` (a source alert mutes matching targets while it fires), and `time_intervals` (recurring mute or active windows). The cost is latency and silence: grouping delays the first notification by `group_wait`, and inhibition deliberately withholds true alerts whose cause is already paging someone.

Version discussed: **Alertmanager 0.33.1**.

## The routing tree walks depth-first, first match wins

The `route` block is a tree. Every alert enters at the **root**, which the configuration reference documents as the entry point for all alerts and which therefore carries no matchers of its own. The walk proceeds **depth-first**: the first child whose **matchers all pass** wins, the alert descends into that child and is then offered to that child's own sub-routes, and **siblings after the winning match are never evaluated**.

Two properties of the walk are load-bearing.

**Inheritance.** A child route inherits every field it does not itself set — `receiver`, `group_by`, all three timers, `mute_time_intervals`. An override set three levels above therefore applies silently to leaves that never mention it. Reading a leaf route in isolation does not determine its behaviour; the path from the root does.

**`continue: true`.** A matching route with `continue: true` does not terminate the sibling scan: the alert is dispatched down that branch *and* the walk resumes with the next sibling. This is the only fan-out mechanism in the tree. An audit branch placed first with `continue: true` copies every matching alert to a webhook and still allows the team branch below it to page.

Because the scan stops at the first match, **matcher order is routing policy**. A general route placed above a specific one shadows it permanently, and the configuration is still valid — nothing reports the dead branch.

## The three timers are per group, not per alert

A **group** is the set of currently firing alerts that share identical values for every label named in **`group_by`** — for example `[alertname, cluster, service]`. Each group is an independent state machine holding its own timers. The special value `'...'` aggregates by all labels, so each distinct alert forms its own group and no aggregation occurs.

| Timer | Default | Governs |
|-------|---------|---------|
| `group_wait` | 30s | Delay between the creation of a **new** group and its first notification |
| `group_interval` | 5m | Minimum delay before notifying again about a group whose **membership changed** (an alert joined or resolved) |
| `repeat_interval` | 4h | Minimum delay before re-sending a notification for a group that has **not changed** |

The sequence for a group's lifetime: the first alert creates the group and starts `group_wait`; alerts arriving inside that window join the group and are delivered in the same notification. After the first notification the group ticks every `group_interval`. On each tick, a membership change produces a notification; an unchanged group produces one only if `repeat_interval` has elapsed. Because repeats are evaluated on group ticks, **`repeat_interval` is effectively rounded up to a multiple of `group_interval`**: setting `repeat_interval: 6m` against `group_interval: 5m` yields repeats every 10 minutes, not every 6.

The practical consequences are asymmetric. A short `group_wait` (10s) reduces the fixed delay on paging routes at the cost of splitting a correlated burst across more notifications. A long `repeat_interval` (24h or more) suits ticket-queue receivers, where a repeat creates a duplicate record rather than a reminder.

## Inhibition: the `equal` list is the join key

An **inhibition rule** suppresses **target** alerts for as long as at least one matching **source** alert is firing. The canonical case is a node-down alert muting the per-service alerts on that node.

```yaml
inhibit_rules:
  - source_matchers: [alertname = NodeDown]
    target_matchers: [severity =~ "warning|info"]
    equal: [cluster, instance]
```

**`equal` is what scopes the rule.** Source and target must carry identical values for every label listed. Omitting it, or listing a label the target alerts do not carry, makes a single firing source match every candidate target — one dead node then mutes the entire fleet's warnings. This is the dominant failure mode of inhibition, and it is silent: the alerts are evaluated and fire in Prometheus, and are never notified.

Evaluation is continuous: each Alertmanager applies the rules to the alerts it currently holds, so when the source resolves the targets un-mute without further action. Inhibition encodes **causal relationships known at configuration time**; it is not a rate limiter. A target of one rule may be the source of another, producing chains whose suppression set is not visible from any single rule.

## Silences and time intervals solve different problems

**Silences** are ad-hoc and data-driven. They are created through the web interface or `amtool silence add`, match on labels, carry an explicit expiry, and are replicated across the high-availability cluster. They suit one-off events such as a planned migration.

**Time intervals** are configuration-driven and recurring. Named schedules live in the top-level `time_intervals` block — the older top-level `mute_time_intervals` section is **deprecated** — and routes reference them either through `mute_time_intervals` (the route is quiet during the window) or `active_time_intervals` (the route notifies only during the window). A recurring rule such as "warnings reach Slack during business hours only" therefore survives in version control rather than depending on a silence being recreated.

## One configuration exercising all four blocks

```yaml
route:
  receiver: default-slack            # root: no matchers, catches unmatched alerts
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [severity = critical]
      receiver: audit-webhook
      continue: true                 # fan-out: record, then keep walking siblings
    - matchers: [team = payments]
      receiver: payments-slack
      routes:
        - matchers: [severity = critical]
          receiver: payments-pagerduty
          group_wait: 10s            # overrides the root's 30s for this leaf only
          repeat_interval: 1h
        - matchers: [severity = warning]
          receiver: payments-slack
          active_time_intervals: [business-hours]
    - matchers: [alertname = Watchdog]
      receiver: deadmans-switch
      repeat_interval: 5m            # heartbeat: absence indicates a broken path

inhibit_rules:
  - source_matchers: [alertname = NodeDown]
    target_matchers: [severity =~ "warning|info"]
    equal: [cluster, instance]       # mute only alerts sharing that node

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

The `Watchdog` route inverts the usual polarity: the alert always fires, so **the absence of its notification** is the signal that the path from Prometheus through Alertmanager to the receiver is broken. It is the only route exercised while the system is healthy.

## High availability: gossip, not load balancing

Alertmanager instances started with `--cluster.peer=am-0:9094 --cluster.peer=am-1:9094` form a **gossip mesh** over the memberlist protocol, replicating silences and the notification log. Each Prometheus server is configured with **all** Alertmanager addresses and sends every alert to **every** peer; placing a load balancer in front defeats the design, because a peer that never receives an alert cannot take over notification for it.

Deduplication happens on the send path rather than the receive path. Peers stagger dispatch by their position in the cluster, and a peer that observes through gossip that a notification has already been sent suppresses its own copy. A partitioned peer stops receiving the others' notification log entries, so the documented failure mode of the design is a duplicated notification rather than a missing one.

This pairs with the earlier SLO burn-rate article: burn-rate rules emit alerts labelled `severity: critical|warning`, and the tree above is where those labels determine delivery.

## Pitfalls

- **There is no fallback path above the root.** An alert that matches no child route is delivered by the root's own `receiver`; if that receiver is a placeholder nobody watches, the alert is dispatched and unread.
- **A general route placed above a specific one shadows it forever.** The scan stops at the first match, the specific route is never evaluated, and configuration validation reports nothing.
- **`continue: true` omitted from an audit branch swallows the alert.** The alert is delivered to the audit webhook and the sibling team routes are never reached, so nobody is paged.
- **An inhibition rule without `equal` mutes fleet-wide.** One firing source suppresses every target matching `target_matchers` regardless of node or cluster; the alerts fire in Prometheus and are never notified.
- **`repeat_interval` shorter than `group_interval` does not shorten repeats.** Repeats are evaluated only on group ticks, so the effective period is `group_interval` rounded up past the configured value.
- **An inherited timer from an ancestor route applies to leaves that never mention it.** A leaf's notification cadence cannot be read from the leaf alone.
- **Load-balancing Prometheus's alert traffic across Alertmanager peers breaks HA.** A peer that never received the alert has no notification to take over, converting the design's duplicate-notification worst case into a missed one.
- **A time interval without `location` is interpreted in UTC.** A window written as local business hours shifts relative to the operators it is meant to cover wherever local time differs from UTC.
