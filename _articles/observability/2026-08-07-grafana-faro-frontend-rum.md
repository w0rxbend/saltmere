---
title: "Grafana Faro: real-user monitoring stitched to backend traces"
date: 2026-08-07
track: observability
summary: "The Faro Web SDK captures browser errors, logs, and Core Web Vitals, and its tracing package emits OpenTelemetry spans that join backend traces over W3C trace context — ingested by Alloy's faro.receiver."
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

**Gist.** Server-side telemetry begins at the request handler, so a slow first paint, a browser-specific JavaScript exception, and a `fetch` that failed before reaching the server leave no trace at all. Grafana Faro is an open-source real-user-monitoring (RUM) software development kit (SDK) that runs in the page, captures browser-native signals, and emits OpenTelemetry (OTel) spans carrying a **W3C `traceparent` header**, so a browser span and the backend spans it causes share one trace identifier. The cost is a public, unauthenticated-by-nature ingest surface reachable from every visitor's browser, plus cross-origin resource sharing (CORS) configuration on every API origin the page calls.

Faro is a signal *source*, not a parallel stack. OTel tracing, the Alloy collector, W3C trace context, and tail sampling all continue to apply unchanged; Faro's role is to originate frontend signals in formats those components already accept.

## The two packages

The distribution is split into a core SDK and a tracing package. The core captures errors, logs, and Web Vitals without loading any tracing code; the tracing package layers OpenTelemetry's browser instrumentation on top.

```bash
npm i @grafana/faro-web-sdk @grafana/faro-web-tracing
```

The version pulled should be pinned and verified against the npm listing, because **the tracing package bundles OpenTelemetry JS and the two packages are released together and must move in lockstep**.

## What the Web SDK captures

`@grafana/faro-web-sdk` is initialized once, as early in page lifetime as possible. The ordering is load-bearing: the instrumentations work by **replacing or wrapping browser globals** (`window.onerror`, `unhandledrejection`, `console`, `fetch`, `XMLHttpRequest`), so anything the application does before `initializeFaro` returns is not observed.

```javascript
import { getWebInstrumentations, initializeFaro } from '@grafana/faro-web-sdk';
import { TracingInstrumentation } from '@grafana/faro-web-tracing';

const faro = initializeFaro({
  // The Alloy faro.receiver endpoint, or a Grafana Cloud Faro Collector URL
  url: 'http://collector-host:12347/collect',
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

`getWebInstrumentations()` returns the default browser bundle. Without further configuration it captures four families of signal:

- **Uncaught errors** — unhandled top-level exceptions and unhandled promise rejections, with stack traces. The frames refer to the *bundled* file, so they are de-minified later, at the collector, against source maps for that bundle.
- **Console** — calls to `console` methods, with which levels are captured controlled by `getWebInstrumentations({ captureConsole: true })` and the related disabled-levels option.
- **Performance** — navigation and resource timing plus Core Web Vitals (Largest Contentful Paint, Cumulative Layout Shift, Interaction to Next Paint, and the rest), sourced from the `web-vitals` library.
- **Sessions and views** — a session-start event, a session identity that ties subsequent signals together, and view-change tracking so signals are grouped by the route in effect when they were emitted.

`app.name`, `app.version`, and `app.environment` are attached as metadata to every signal the instance emits, which is what makes a release comparison possible after the fact: **the version recorded on an error is the version of the bundle that produced it**, not the version currently deployed.

Manual emission goes through `faro.api`: `pushError()`, `pushLog()`, `pushEvent()`, and `pushMeasurement()`. Domain events — a checkout completion, a feature-flag branch taken — enter the same pipeline as the automatic capture and inherit the same session and view metadata.

## Tracing: where frontend meets backend

`TracingInstrumentation()` instruments `fetch` and `XMLHttpRequest` using OpenTelemetry's browser instrumentation. Each outbound request produces a client span, and the instrumentation **injects a W3C `traceparent` header onto that request by default**.

That header is the same [W3C trace context](/observability/) mechanism the backend already reads. When the request arrives, server-side OTel instrumentation finds an existing `traceparent`, adopts the trace identifier and the incoming span identifier as its parent, and continues the trace rather than starting a new root. The resulting trace begins with a browser span (`HTTP GET /api/cart`) and continues into the gateway, the service, and the database call. No correlation step is performed anywhere: the join is a consequence of both ends implementing the same header format.

Two constraints follow from running propagation inside a browser rather than a server:

- **CORS applies to request headers.** For a cross-origin API call, the browser sends a preflight request and refuses to attach `traceparent` (and `tracestate`) unless the target origin lists them in `Access-Control-Allow-Headers`. The symptom is a request that succeeds while the backend span appears as a separate root trace — or, with a stricter preflight, does not succeed at all.
- **Propagation is scoped by origin.** Trace identifiers are attached to same-origin requests; cross-origin targets must be listed explicitly (`propagateTraceHeaderCorsUrls` on the tracing instrumentation), so requests to third-party endpoints do not carry internal identifiers outward by default.

## The ingest side: Alloy's `faro.receiver`

Faro posts to any endpoint that accepts the Faro payload format. Self-hosted, that endpoint is the **`faro.receiver` component** in [Grafana Alloy](/observability/). It terminates the browser's HTTP POSTs, optionally de-minifies stack traces against downloaded source maps, and fans signals out by type — logs and events to Loki, traces to Tempo.

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

The `url` passed to `initializeFaro` addresses this server's `/collect` path — here `http://collector-host:12347/collect`. `cors_allowed_origins` governs which page origins the browser is permitted to post from; it is an origin allowlist enforced by the browser, not an authentication mechanism, so the receiver still faces the open internet and benefits from a fronting proxy that applies rate limiting.

Once traces reach Tempo they are ordinary spans. Downstream concerns — [tail sampling](/observability/), retention, service graphs — treat frontend and backend spans identically, which is the point of encoding them in OTel at the edge rather than in a Faro-specific format.

**Grafana Cloud Frontend Observability** provides a hosted ingest endpoint and pre-built dashboards as an alternative to running the collector. The browser-side code is unchanged; only the `url` differs.

## Pitfalls

- **`initializeFaro` called after application bootstrap.** Errors thrown during startup — the most diagnostically valuable ones — are missing entirely, because the global handlers were installed after they fired.
- **Source maps not uploaded or not reachable.** Stack traces arrive as minified frames (`a.b is not a function` at `main.4f2c.js:1:88213`) and remain unreadable; `sourcemaps { download = true }` only helps if the maps are fetchable from the bundle's origin.
- **`traceparent` absent from `Access-Control-Allow-Headers` on a cross-origin API.** The browser strips the header, the server starts a fresh root trace, and the frontend and backend spans appear as two unrelated traces with no error reported on either side.
- **Tracing package and core SDK versions drifting apart.** The tracing package bundles OpenTelemetry JS; mismatched versions break at the point where the tracing instrumentation registers with the core instance.
- **Receiver exposed without rate limiting.** The endpoint is called directly by every visitor's browser and the `apiKey` ships in the page bundle, so the write path is reachable by anyone who reads the JavaScript.
- **`captureConsole` enabled on a chatty application.** Every `console.info` becomes a log line billed and stored in Loki, at browser-session volume rather than server volume.
