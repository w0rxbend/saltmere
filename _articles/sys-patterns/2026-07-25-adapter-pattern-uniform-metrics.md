---
title: "The adapter pattern: a uniform monitoring interface over heterogeneous services"
date: 2026-07-25
track: sys-patterns
summary: "The third single-node pattern in Burns completes the trio. Where a sidecar adds capability and an ambassador reshapes outbound calls, an adapter reshapes what the outside world sees — turning a service's native, non-standard output into the uniform interface a platform expects."
reading_time: 5
tags: [adapter, sidecar, single-node-patterns, containers, prometheus, burns]
sources:
  - title: "Burns, Designing Distributed Systems (2nd ed.) — single-node patterns: sidecar, ambassador, adapter"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Designing Distributed Systems (free ebook, Microsoft)"
    url: "https://info.microsoft.com/rs/157-GQE-382/images/EN-CNTNT-eBook-DesigningDistributedSystems.pdf"
  - title: "Prometheus exposition format & client library basics"
    url: "https://prometheus.io/docs/instrumenting/exposition_formats/"
---

**Gist.** A monitoring or logging platform requires every service to expose the same interface, but a real fleet contains services that cannot be modified: legacy applications that write only a text log, third-party binaries with a bespoke status page, databases with their own statistics command. The adapter pattern places a second container beside each such service in the same deployment unit; that container reads the service's native output and re-publishes it in the platform's standard format, leaving the service's own code untouched. The cost is one additional container process per service instance, with its own memory footprint, its own failure mode, and a translation step that can silently go stale while the adapter still answers scrapes.

## Position among the single-node patterns

Burns groups three single-node, multi-container patterns, distinguishable by the direction each faces. A **sidecar** adds a capability to the main container. An **ambassador** intercepts the main container's *outbound* calls and reshapes them. An **adapter** faces the opposite direction from the ambassador: it reshapes what the main container exposes *inbound*, to the outside world, so that a heterogeneous fleet presents one uniform surface.

All three share the same enabling mechanism: **co-scheduling**. The two containers are placed on the same node in the same deployment unit, so they share a network namespace (localhost) and can share a filesystem volume. That shared namespace is what makes translation possible without a network hop and without either container needing an address for the other.

## A concrete adapter: text log to Prometheus exposition format

Consider a service whose only sign of health is a line appended to a log file:

```
2026-07-25T09:14:02Z requests=1840 errors=12 p99_ms=230
```

Prometheus scrapes an HTTP endpoint and parses the [exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/): one sample per line, `metric_name{labels} value [timestamp]`, with the label set and the timestamp both optional, preceded where present by `# HELP` and `# TYPE` comment lines. The log above satisfies none of that. Rather than modify the application, an adapter container in the same pod tails the log, keeps the most recently parsed values in memory, and serves them on demand:

```python
# adapter.py — runs beside the app, shares the log volume
import re, time
from http.server import BaseHTTPRequestHandler, HTTPServer

LINE = re.compile(r"requests=(\d+) errors=(\d+) p99_ms=(\d+)")
state = {"requests": 0, "errors": 0, "p99": 0}

def tail(path):
    with open(path) as f:
        f.seek(0, 2)                      # jump to end
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.5); continue
            m = LINE.search(line)
            if m:
                state["requests"], state["errors"], state["p99"] = map(int, m.groups())

class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            f"# TYPE app_requests_total counter\n"
            f"app_requests_total {state['requests']}\n"
            f"# TYPE app_errors_total counter\n"
            f"app_errors_total {state['errors']}\n"
            f"# TYPE app_request_p99_ms gauge\n"
            f"app_request_p99_ms {state['p99']}\n"
        ).encode()
        self.send_response(200)
        # Prometheus 3.0 onwards fails the scrape without a valid Content-Type
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers(); self.wfile.write(body)

# run tail() in a thread, then HTTPServer(("", 9109), Metrics).serve_forever()
```

Two mechanisms are load-bearing here and neither is incidental to the pattern.

**The translation is decoupled in time from the scrape.** The tailing thread advances `state` whenever the application writes a line; the HTTP handler reads whatever `state` holds at the moment Prometheus asks. **The adapter therefore always returns a well-formed response, even when the underlying source has stopped producing data** — the endpoint reports the last observed values rather than an error. The exposition format permits an optional per-sample timestamp, but an adapter that omits it — as this one does — leaves Prometheus to stamp each sample with the scrape time, so a stalled adapter and a healthy one produce identically fresh-looking series.

**Counters and gauges are not interchangeable.** `app_requests_total` is declared a counter, meaning Prometheus assumes it only increases and computes rates from differences between scrapes; `app_request_p99_ms` is a gauge, a value that may move in either direction. The adapter's parse must preserve that distinction, because the source log carries no type information at all — the type is supplied entirely by the adapter's `# TYPE` lines.

## Deployment shape

In Kubernetes the arrangement is two entries under `containers:` in one pod, joined by an `emptyDir` volume mounted at the same path in both:

```yaml
containers:
  - name: legacy-app
    image: legacy-app:1.4
    volumeMounts: [{ name: logs, mountPath: /var/log/app }]
  - name: metrics-adapter
    image: metrics-adapter:1.0        # runs adapter.py
    ports: [{ containerPort: 9109 }]
    volumeMounts: [{ name: logs, mountPath: /var/log/app }]
volumes:
  - name: logs
    emptyDir: {}
```

The application continues writing plain text; Prometheus scrapes the adapter on port 9109. An `emptyDir` volume shares the lifetime of the pod, so the log is a transport between the two containers, not durable storage.

## What the separation buys

The application team ships no new code and takes on no new dependency. The adapter is versioned, built and rolled out on an independent cadence: changing the monitoring format across a fleet means republishing one adapter image rather than re-releasing every service. Because the adapter's outward interface is identical regardless of what sits behind it — a Java application, a Go binary, a shell script — the platform observes a genuinely uniform surface, and a single scrape configuration and alerting ruleset applies to all of them.

The same decoupling argument underlies the sidecar and the ambassador. The adapter applies it to the outward-facing interface specifically.

A second adapter can be attached to the same unmodified container — one reshaping the log into Prometheus samples, another reshaping it into structured JSON on stdout for a log collector. Both read the same source; neither requires the application to know either exists.

## Pitfalls

- **A crashed or wedged tailing thread leaves the endpoint serving stale values indefinitely.** The HTTP handler reads shared state and never consults the log, so the scrape succeeds and the metric flatlines rather than disappearing. An alert on absence of data will not fire; only an alert on an unchanging counter will.
- **Restarting the adapter resets in-memory counters to zero while the application's own totals continue rising.** Prometheus interprets a counter that drops as a reset and adjusts rate calculations accordingly, but the absolute totals reported by the adapter no longer correspond to the application's lifetime totals.
- **A parse that misses a line is silent.** The regular expression matches or it does not; an unmatched line leaves the previous values in place, so a change to the application's log format degrades to a permanently frozen metric rather than a startup failure.
- **`emptyDir` is deleted when the pod is removed.** Any log lines the adapter has not yet read at pod teardown are lost, and there is no replay.
- **The adapter is a second process to supervise.** Without a liveness probe on the adapter container, the pod remains "ready" while its metrics endpoint is unreachable, and the loss of visibility is itself invisible.
- **Type declarations live only in the adapter.** Mislabelling a monotonically increasing value as a gauge, or a fluctuating one as a counter, produces plausible-looking graphs whose rate and increase computations are meaningless.
