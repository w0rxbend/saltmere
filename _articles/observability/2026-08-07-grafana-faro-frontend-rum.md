---
title: "Grafana Faro: real-user monitoring that stitches to your backend traces"
date: 2026-08-07
track: observability
summary: "Instrument a web app with the Faro Web SDK to capture errors, logs, and Core Web Vitals, then add OTel tracing so browser spans join your backend traces over W3C tracecontext — ingested by Alloy's faro.receiver."
reading_time: 6
tags: [grafana-faro, rum, frontend-observability, opentelemetry, web-vitals]
sources:
  - title: "Faro Web SDK — README (grafana/faro-web-sdk)"
    url: "https://github.com/grafana/faro-web-sdk/blob/main/packages/web-sdk/README.md"
  - title: "Faro quick start for the browser"
    url: "https://github.com/grafana/faro-web-sdk/blob/main/docs/sources/tutorials/quick-start-browser.md"
  - title: "faro.receiver — Grafana Alloy component reference"
    url: "https://grafana.com/docs/alloy/latest/reference/components/faro/faro.receiver/"
  - title: "Frontend Observability instrumentation setup — Grafana Cloud"
    url: "https://grafana.com/docs/grafana-cloud/monitor-applications/frontend-observability/instrument/faro/"
  - title: "@grafana/faro-web-sdk — npm"
    url: "https://www.npmjs.com/package/@grafana/faro-web-sdk"
---

Most of your telemetry stops at the server's front door. You have traces, logs, and metrics from the API inward, but the slow first paint, the JavaScript exception that only fires on Safari, and the failed `fetch` that the user actually saw are all invisible. Grafana Faro closes that gap. It is an open-source real-user-monitoring (RUM) SDK that runs in the browser, captures what the page does, and ships it to a collector as errors, logs, events, Core Web Vitals, and — crucially — OpenTelemetry traces that connect to your backend spans.

Faro is a distinct signal *source* in the observability stack. The sibling pieces — OTel tracing, the Alloy collector, W3C trace context, tail sampling — all still apply. Faro's job is to originate frontend signals and hand them off in formats those pieces already understand.

## The two packages

Faro is split so you only pay for what you use. The core SDK captures errors, logs, and Web Vitals with no tracing overhead; the tracing package layers OpenTelemetry on top.

```bash
npm i @grafana/faro-web-sdk @grafana/faro-web-tracing
```

The current release line is the 2.x series (`2.8.2`, published 2026-07-01). Verify the version you pull, because the tracing package bundles OpenTelemetry JS and the two must stay in lockstep.

## What the Web SDK captures out of the box

`@grafana/faro-web-sdk` is initialized once, as early as possible, so it can hook the browser globals before your app runs:

```javascript
import { getWebInstrumentations, initializeFaro } from '@grafana/faro-web-sdk';
import { TracingInstrumentation } from '@grafana/faro-web-tracing';

const faro = initializeFaro({
  // The Faro Collector / Alloy faro.receiver endpoint, or a Grafana Cloud URL
  url: 'https://collector-host:12347/collect',
  apiKey: 'secret',
  app: {
    name: 'frontend',
    version: '1.0.0',
    environment: 'production',
  },
  instrumentations: [
    // Errors, console, Web Vitals, session tracking, page views
    ...getWebInstrumentations(),
    // OpenTelemetry tracing for fetch/XHR, with W3C tracecontext propagation
    new TracingInstrumentation(),
  ],
});
```

`getWebInstrumentations()` returns the default browser instrumentation bundle. With no extra config it captures:

- **Uncaught errors** — unhandled top-level exceptions and unhandled promise rejections, with stack traces (symbolicated later via source maps at the collector).
- **Console** — `console.error`, `warn`, and `info` calls, opt-in via `getWebInstrumentations({ captureConsole: true })`.
- **Performance** — navigation and resource timing plus Core Web Vitals (LCP, CLS, INP, and the rest) sourced from the browser's `web-vitals` reporting.
- **Sessions and views** — a session-start event, session identity that ties events together, and view-change tracking so signals are grouped by the route the user was on.

Beyond the automatic capture, `faro.api` gives you manual hooks: `faro.api.pushError()`, `faro.api.pushLog()`, `faro.api.pushEvent()`, and `faro.api.pushMeasurement()`. That is how you record domain-specific events — a checkout completion, a feature-flag branch — alongside the automatic telemetry.

## Tracing: where frontend meets backend

The single most valuable thing Faro does is turn a user click into a trace that continues into your services. `TracingInstrumentation()` from `@grafana/faro-web-tracing` instruments `fetch` and `XMLHttpRequest` using OpenTelemetry's browser instrumentation. Every outbound request becomes a client span, and — this is the load-bearing detail — the instrumentation injects **W3C `traceparent` headers** onto those requests by default.

That header is the same [W3C trace context](/observability/) mechanism your backend already reads. When the request lands, your server-side OTel instrumentation sees an existing `traceparent`, adopts the trace and parent-span IDs, and continues the trace rather than starting a fresh one. The result is a single trace that begins with a browser span (`HTTP GET /api/cart`) and flows straight into the API gateway, the service, and the database call — full-stack tracing with no manual correlation. Faro doesn't reinvent propagation; it reuses the standard so the join is automatic.

One practical note: browsers enforce CORS on custom headers, so cross-origin API calls need the server to allow the `traceparent` (and `tracestate`) request headers, and you typically scope propagation to your own origins so you're not leaking trace IDs to third parties.

## The ingest side: Alloy's `faro.receiver`

Faro ships to any endpoint that speaks the Faro payload format. In self-hosted setups that endpoint is the **Faro Collector**, available as the `faro.receiver` component in [Grafana Alloy](/observability/). It terminates the browser's HTTP POSTs, optionally de-minifies stack traces with source maps, and fans the signals out — logs and events to Loki, traces to Tempo:

```alloy
faro.receiver "default" {
  server {
    listen_address       = "0.0.0.0"
    listen_port          = 12347
    cors_allowed_origins = ["https://myapp.example.com"]
  }

  sourcemaps {
    download = true
  }

  output {
    logs   = [loki.write.default.receiver]
    traces = [otelcol.exporter.otlp.default.input]
  }
}

loki.write "default" {
  endpoint {
    url = "https://loki.example.com/loki/api/v1/push"
  }
}

otelcol.exporter.otlp "default" {
  client {
    endpoint = "tempo.example.com:4317"
  }
}
```

The `url` in your `initializeFaro` call points at this server's `/collect` path (here, `http://collector-host:12347/collect`). Because the browser is a public client, front the receiver with something that handles CORS and, ideally, rate limiting. Once traces reach Tempo they are ordinary spans, so downstream concerns — [tail sampling](/observability/), retention, service graphs — treat frontend and backend spans identically.

If you'd rather not run the collector, **Grafana Cloud Frontend Observability** provides a hosted ingest endpoint and pre-built dashboards; you swap the `url` for the Cloud endpoint and keep the same SDK code. The instrumentation in the browser is unchanged either way — only the destination differs.

## Why this shape matters

The design decision worth internalizing is that Faro is deliberately thin at the edge and standard on the wire. It captures browser-native signals (Web Vitals, uncaught errors, sessions) that no backend can see, then encodes traces in OTel and propagates context in W3C tracecontext — formats your existing pipeline already ingests. You don't get a parallel, siloed frontend monitoring product; you get the missing spans and logs slotted into the same Loki and Tempo backends your services already write to, correlated by trace ID and session.

**Try next:** stand up an Alloy `faro.receiver` locally on port 12347, point `initializeFaro` at it with `TracingInstrumentation()` enabled, trigger a `fetch` from the page, and open the resulting trace in Tempo to confirm the browser span and your backend span share one trace ID.
