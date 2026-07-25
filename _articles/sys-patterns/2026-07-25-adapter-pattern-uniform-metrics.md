---
title: "The adapter pattern: make a messy service speak your monitoring's language"
date: 2026-07-25
track: sys-patterns
summary: "The third single-node pattern in Burns completes the trio. Where a sidecar adds capability and an ambassador reshapes outbound calls, an adapter reshapes what the outside world sees — turning a service's native, non-standard output into the uniform interface your platform expects."
reading_time: 4
tags: [adapter, sidecar, single-node-patterns, containers, prometheus, burns]
sources:
  - title: "Burns, Designing Distributed Systems (2nd ed.) — single-node patterns: sidecar, ambassador, adapter"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Designing Distributed Systems (free ebook, Microsoft)"
    url: "https://info.microsoft.com/rs/157-GQE-382/images/EN-CNTNT-eBook-DesigningDistributedSystems.pdf"
  - title: "Prometheus exposition format & client library basics"
    url: "https://prometheus.io/docs/instrumenting/exposition_formats/"
---

Burns groups three single-node, multi-container patterns, and they're easy to tell apart by *which direction they face*. A **sidecar** adds a capability to the main container (last covered). An **ambassador** sits in front of the main container's *outbound* calls and reshapes them. An **adapter** faces the *other* way: it reshapes what the main container exposes *to the outside world*, so a heterogeneous fleet presents one uniform interface.

That uniform interface is the whole point. Your monitoring system wants every service to expose metrics one way. Your log pipeline wants one format. But real systems are a museum: a legacy app that only writes a text log, a third-party binary with a bespoke `/status` page, a database with its own stats command. You can't rewrite them all. You put an adapter next to each one.

## A concrete adapter: legacy app → Prometheus

Say you run an old service whose only sign of health is a line it appends to a log file:

```
2026-07-25T09:14:02Z requests=1840 errors=12 p99_ms=230
```

Prometheus can't scrape that. Rather than patch the app, deploy an adapter container in the same pod that tails the log and exposes a proper `/metrics` endpoint in the [exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/):

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
        self.send_response(200); self.end_headers(); self.wfile.write(body)

# run tail() in a thread, then HTTPServer(("", 9109), Metrics).serve_forever()
```

The two containers share a log volume; the app keeps writing plain text, and Prometheus scrapes the adapter on `:9109`. In a Kubernetes pod that's two entries under `containers:` sharing an `emptyDir`:

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

## Why this beats "just add a library"

The app team ships no new code and takes no new dependency. The adapter is versioned, tested, and rolled out on its *own* cadence — swap the monitoring format across the whole fleet by bumping one image, not by re-releasing every service. And because the adapter is the same regardless of what's behind it (Java app, Go binary, shell script), your platform sees a genuinely uniform surface. That decoupling is exactly what made the sidecar and ambassador worth it too; the adapter just applies it to the *outward-facing* interface.

The trade-off is the same as its siblings: an extra container per instance costs a little memory and one more thing to monitor, and the adapter can lag or crash independently — so give it a liveness probe and alert if `/metrics` stops updating.

**Try next:** write a second adapter for the *same* app that reshapes its log into structured JSON on stdout for a log collector. Running two adapters against one unmodified container is the pattern clicking into place — capability and interface, cleanly separated from the code that does the actual work.
