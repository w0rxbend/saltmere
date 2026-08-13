---
title: "Real-time delivery at scale: long-polling vs SSE vs WebSocket (and where WebTransport now fits)"
date: 2026-08-13
track: sys-patterns
summary: "Every \"design a chat system\" interview hinges on one choice: how the server pushes to a million clients. Long-polling, Server-Sent Events, and WebSocket differ less in speed than in connection-state cost, proxy behavior, and how you recover missed messages after a reconnect."
reading_time: 6
tags: [websocket, server-sent-events, long-polling, realtime, system-design]
sources:
  - title: "MDN — Using server-sent events"
    url: "https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events"
  - title: "RFC 6455 — The WebSocket Protocol"
    url: "https://datatracker.ietf.org/doc/html/rfc6455"
  - title: "Slack Engineering — Migrating Millions of Concurrent Websockets to Envoy"
    url: "https://slack.engineering/migrating-millions-of-concurrent-websockets-to-envoy/"
  - title: "Ably — What is HTTP Long Polling?"
    url: "https://ably.com/topic/long-polling"
  - title: "MDN — WebTransport API"
    url: "https://developer.mozilla.org/en-US/docs/Web/API/WebTransport_API"
---

HTTP is pull; chat, notifications, and live dashboards are push. Three mechanisms fake or fix that, and the interview question "WebSocket or SSE?" is really asking whether you understand what each one costs at the load balancer and at hop number one million.

## Protocol mechanics in one paragraph each

**Long-polling** is plain HTTP: the client sends a GET, the server *holds* it open until it has data (or a ~30 s timeout), responds, and the client immediately re-requests. It works through every proxy ever built, which is why it survives as the universal fallback — but each message pays full request/response overhead, and [each held request ties up server resources](https://ably.com/topic/long-polling) while delivering nothing.

**Server-Sent Events (SSE)** is one long-lived HTTP response with `Content-Type: text/event-stream`. The server writes `id:`/`event:`/`data:` lines; the browser's `EventSource` parses them. It's server→client only, but reconnection is *built into the protocol*: the browser auto-reconnects and sends the last seen `id` back as a [`Last-Event-ID` header](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events), so backfill is free if your server can replay from an ID. One sharp edge: over HTTP/1.1, browsers cap ~6 connections per origin, so a multi-tab app wants SSE over HTTP/2, where streams multiplex on one connection.

**WebSocket** ([RFC 6455](https://datatracker.ietf.org/doc/html/rfc6455)) starts as an HTTP/1.1 GET with `Upgrade: websocket`; after the `101` handshake the TCP connection becomes a symmetric, bidirectional framed pipe — 2–14 bytes of frame header per message, masked client-to-server, with ping/pong control frames for liveness. Full duplex and lowest per-message overhead, but *everything* HTTP gave you — routing, retries, reconnection, caching — is gone, and you rebuild it in application code.

**WebTransport** rides HTTP/3/QUIC and offers multiple independent streams plus unreliable datagrams (no head-of-line blocking, useful for games/media). Status check as of mid-2026: Chrome, Edge and Firefox have shipped it for years, and Safari 26.4 (March 2026) completed [cross-browser availability](https://developer.mozilla.org/en-US/docs/Web/API/WebTransport_API). It needs an HTTP/3 path end-to-end, which most corporate proxies and LBs still don't give you — a nod, not yet a default.

## What a million connections actually costs

Persistent connections shift cost from per-message to per-connection *state*: a file descriptor, kernel socket buffers, TLS session, heartbeat timer, and userspace session object per client — tens of KB each, so 1M idle clients is tens of GB of mostly-idle state spread across a gateway tier. The real limits are operational. Every LB in the path holds the same state, and each LB↔backend pair has ~64K ephemeral ports. Deploys become the hard part: restarting a gateway drops every connection it holds, and a million clients reconnecting at once is a thundering herd aimed at your auth stack — Slack's [Envoy migration](https://slack.engineering/migrating-millions-of-concurrent-websockets-to-envoy/) write-up is largely about exactly this: draining millions of WebSockets slowly and surviving mass-reconnect storms.

Proxy behavior differs per mechanism. Idle timeouts kill quiet connections: AWS ALB defaults to 60 s idle, nginx `proxy_read_timeout` likewise — so WebSockets need ping/pong inside the timeout, and SSE needs periodic `: keepalive` comment lines. SSE additionally requires buffering off (`X-Accel-Buffering: no` for nginx), or your events sit in a proxy buffer. And because a client's connection lands on *one* gateway node but a message for them can originate anywhere, you need a **pub/sub backplane** (Redis pub/sub, Kafka, NATS): publishers write to a channel, every gateway subscribes and fans out to its local sockets. Sticky routing then matters only for in-flight session state, not correctness.

Reconnect/backfill is the part candidates forget. SSE gives you `Last-Event-ID` natively. WebSocket gives you nothing: you implement resume tokens — a per-session monotonically increasing sequence number the client echoes on reconnect so the server can replay from a short retained buffer, falling back to full state resync when the buffer has aged out.

## Comparison table

| | Long-polling | SSE | WebSocket | WebTransport |
|---|---|---|---|---|
| Direction | server→client | server→client | bidirectional | bidirectional + datagrams |
| Transport | HTTP request cycle | HTTP stream (best over H2) | TCP after `Upgrade` | HTTP/3 / QUIC |
| Per-message overhead | full headers | few bytes framing | 2–14 B frame header | QUIC frames |
| Reconnect + backfill | inherent (it's polling) | built-in, `Last-Event-ID` | DIY resume tokens | DIY |
| Proxy/LB friction | none | buffering, idle timeouts | `Upgrade` support, idle timeouts | needs end-to-end HTTP/3 |
| Best for | fallback, low-frequency | feeds, notifications, LLM token streams | chat, collaboration, games | media, latency-critical |

## Minimal working SSE (Node, no dependencies)

```js
// server.mjs — run: node server.mjs
import http from "node:http";
let id = 0;
http.createServer((req, res) => {
  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    "x-accel-buffering": "no",              // stop nginx buffering the stream
  });
  const last = req.headers["last-event-id"]; // resume point after reconnect
  if (last) res.write(`: client resumed after event ${last}\n\n`);
  const t = setInterval(() =>
    res.write(`id: ${++id}\nevent: tick\ndata: {"ts":${Date.now()}}\n\n`), 2000);
  req.on("close", () => clearInterval(t));   // client gone: free the state
}).listen(8080);
```

```js
// client (browser console) — reconnection and Last-Event-ID are automatic
const es = new EventSource("http://localhost:8080/");
es.addEventListener("tick", e => console.log(e.lastEventId, e.data));
```

Kill the server mid-stream and restart it: the browser reconnects on its own and you'll see the resume comment with the last delivered ID. That's the whole backfill contract, handed to you by the protocol.

## Picking, in one sentence each

Notifications, activity feeds, token streaming: SSE — you get reconnect semantics free and it's just HTTP. Chat, presence, collaborative editing: WebSocket — you need client→server on the same pipe, and you accept building resume tokens and heartbeats. Hostile networks or ancient infrastructure: long-polling as the fallback tier your library (Socket.IO-style) degrades to. All three share the same backend shape — stateless-ish gateway tier, pub/sub backplane, replayable log for backfill — which is the actual answer to the interview question.

**Try next:** run the SSE server above behind nginx, remove the `x-accel-buffering` header, and watch events stall in the proxy buffer — then add it back and confirm delivery is immediate; you've just debugged the most common SSE production bug on your laptop.
