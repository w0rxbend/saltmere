---
title: "Real-Time to the Browser: Polling vs SSE vs WebSockets"
date: 2026-08-10
track: microservices
summary: The "design a live feed / chat / notifications" interview asks one thing under the hood — how do bytes get from server to client without a page refresh? Short polling, long polling, Server-Sent Events, and WebSockets, compared by direction, transport, overhead, reconnection, and proxy-friendliness, with a decision table and working snippets.
reading_time: 6
tags:
  - realtime
  - websocket
  - sse
  - http
  - scaling
sources:
  - title: "Using server-sent events (MDN Web Docs)"
    url: "https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events"
  - title: "EventSource (MDN Web Docs)"
    url: "https://developer.mozilla.org/en-US/docs/Web/API/EventSource"
  - title: "Server-sent events (WHATWG HTML Standard, §9.2)"
    url: "https://html.spec.whatwg.org/multipage/server-sent-events.html"
  - title: "RFC 6455 — The WebSocket Protocol"
    url: "https://www.rfc-editor.org/rfc/rfc6455.html"
  - title: "The WebSockets API (MDN Web Docs)"
    url: "https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API"
---

Every "design a live feed," "design chat," or "design notifications" question collapses to one problem: HTTP was built for the client to ask and the server to answer, but you need the server to talk first. There are four production answers, and interviewers want you to reach for the cheapest one that meets the requirement — not the most impressive. Here they are in ascending order of power and cost.

## Short polling

The client asks on a timer: `GET /messages?since=...` every N seconds. If there's nothing new, the server returns an empty result and you paid for a full request/response round trip anyway.

- **Direction:** client-initiated pull, one shot per request.
- **Transport:** ordinary HTTP request/response. Nothing special.
- **Overhead:** worst of the bunch. Every poll carries full headers, cookies, and TLS resumption cost; most polls return nothing. Latency is bounded by your interval — a 5-second poll means up to 5 seconds of staleness.
- **Reconnection / back-pressure:** trivially handled — each request is independent, and a slow client just polls less often. There's no connection to drop.
- **Proxy/firewall friendliness:** perfect. It's plain HTTP; every cache, proxy, and corporate firewall understands it.

Choose it when updates are infrequent and slightly stale is fine (a dashboard that refreshes every 30s, checking a job's status). It's also the honest baseline you compare everything else against.

## Long polling

The client sends a request and the server *holds it open* until data is available (or a timeout fires), then responds. The client immediately issues the next request. You've simulated push over request/response.

- **Direction:** still client-initiated, but the response is deferred to the moment of a real event — so effective latency approaches real-time.
- **Transport:** HTTP request/response with a held connection. Each message is one response; then you reconnect.
- **Overhead:** far better than short polling for low-frequency events (no empty responses), but every message still costs a fresh request with full headers, and a burst of events degrades toward short polling.
- **Reconnection / back-pressure:** the reconnect loop is built into the pattern. Back-pressure is naturally applied — the client can't be flooded because it only re-arms after processing the last response.
- **Proxy/firewall friendliness:** excellent, again because it's normal HTTP; just make sure intermediary read timeouts are longer than your hold window.

Long polling's real modern role is a **fallback**: when SSE or WebSockets are blocked by a hostile proxy, libraries like Socket.IO downgrade to long polling so the feature still works. Know it for that.

## Server-Sent Events (SSE)

SSE is a one-way, server→client stream over a single long-lived HTTP response with `Content-Type: text/event-stream`. The server never closes the body; it keeps writing newline-delimited events. The browser side is the built-in `EventSource`.

- **Direction:** **one-way, server→client only.** The client still uses ordinary HTTP requests for anything it wants to send upstream.
- **Transport:** one long-lived HTTP/1.1 or HTTP/2 response — HTTP *streaming*, not a protocol upgrade. It stays HTTP the whole way.
- **Overhead:** one connection amortizes all messages; each event is a few bytes of `data:`/`event:`/`id:` text plus the payload. Text only (UTF-8); binary needs base64, which is a real downside for media.
- **Reconnection / back-pressure:** SSE's superpower. **Reconnection is automatic** — if the connection drops, the browser reconnects on its own. Each event may carry an `id:`; on reconnect the browser sends a `Last-Event-ID` HTTP request header so the server can resume where it left off, and the `retry:` field sets the reconnection delay in milliseconds. Resumable delivery for free — exactly what a live feed wants.
- **Proxy/firewall friendliness:** good — it's HTTP — but the classic gotcha is the **per-domain connection cap**. On HTTP/1.1 a browser allows only about **6 open connections per domain**, and each `EventSource` eats one; open a few tabs and you exhaust the pool (Chrome and Firefox both mark this "won't fix"). Under **HTTP/2 this limit effectively disappears**: streams are multiplexed over one TCP connection, the max negotiated between client and server (commonly defaulting to 100). If you ship SSE, ship it over HTTP/2.

Choose SSE for feeds, notifications, live scores, log tailing, progress bars, and token-streaming LLM output — anything where the data flows *down* and the client rarely needs a low-latency upstream channel.

A concrete endpoint (Node/Express), including a keep-alive comment and resumable ids:

```js
app.get("/events", (req, res) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
  });

  // Resume support: client sends Last-Event-ID on reconnect
  const lastId = Number(req.headers["last-event-id"] || 0);

  let id = lastId;
  const timer = setInterval(() => {
    res.write(`id: ${++id}\n`);
    res.write(`event: price\n`);
    res.write(`data: ${JSON.stringify({ id, value: Math.random() })}\n\n`);
  }, 1000);

  // Comment line = heartbeat that keeps proxies from idling us out
  const ka = setInterval(() => res.write(`: keep-alive\n\n`), 15000);

  req.on("close", () => { clearInterval(timer); clearInterval(ka); });
});
```

Client side is three lines, with reconnection handled for you:

```js
const es = new EventSource("/events");
es.addEventListener("price", (e) => render(JSON.parse(e.data)));
```

## WebSockets

WebSocket (RFC 6455) gives you **full-duplex** communication — both sides send at any time — over a single persistent TCP connection. It begins life as HTTP: the client sends an `Upgrade: websocket` / `Connection: Upgrade` request with a `Sec-WebSocket-Key`, the server answers **101 Switching Protocols** with the hashed `Sec-WebSocket-Accept`, and after that the socket speaks the WebSocket framing protocol, not HTTP.

- **Direction:** **bidirectional.** This is the only option where the client can push to the server with the same low latency the server pushes to it.
- **Transport:** a persistent, upgraded TCP connection. After the handshake, HTTP semantics are gone — no per-request headers, no status codes, no HTTP caching. Frames carry **text or binary** natively (client→server frames are masked; server→client frames are not).
- **Overhead:** lowest per-message cost — a few bytes of frame header, no HTTP headers or cookies re-sent. Ideal for chatty, high-frequency, small messages.
- **Reconnection / back-pressure:** **you build this yourself.** The protocol gives you ping/pong control frames, but heartbeat cadence, reconnect-with-backoff, and message replay after a drop are your code. There's no `Last-Event-ID` equivalent. Flow control exists at the TCP level, but application back-pressure (a slow consumer, an unbounded send buffer) is your responsibility — see the companion note on [backpressure and flow control](/articles/sys-patterns/2026-07-31-backpressure-flow-control).
- **Proxy/firewall friendliness:** the weakest. Some older proxies and corporate firewalls don't understand the Upgrade and will break or buffer the connection; `wss://` (TLS) fares much better because intermediaries can't inspect and mangle the stream. This is why you keep long polling as a fallback.

Choose WebSockets when you genuinely need a low-latency *upstream* channel: chat, multiplayer games, collaborative editing, trading, live cursors.

A minimal server handler (`ws`):

```js
import { WebSocketServer } from "ws";
const wss = new WebSocketServer({ port: 8080 });

wss.on("connection", (ws) => {
  ws.isAlive = true;
  ws.on("pong", () => { ws.isAlive = true; });   // heartbeat you own
  ws.on("message", (buf) => {
    const msg = JSON.parse(buf);
    // fan out to everyone (see scaling note below)
    for (const c of wss.clients) if (c.readyState === 1) c.send(JSON.stringify(msg));
  });
});

// You write the liveness sweep; the protocol won't do it for you
setInterval(() => {
  for (const ws of wss.clients) {
    if (!ws.isAlive) return ws.terminate();
    ws.isAlive = false; ws.ping();
  }
}, 30000);
```

## Scaling the stateful ones

Short and long polling scale like any stateless HTTP endpoint — put them behind a load balancer and forget them. SSE and WebSockets are different: the connection is **long-lived and pinned to one server process**, so you need two things.

1. **Sticky sessions.** A given client's connection must stay on the server that holds it. The load balancer routes by connection, not per-request.
2. **A pub/sub fan-out.** With connections spread across many servers, a message that must reach a user connected to *another* box can't be delivered by that box alone. Put a broker in the middle — **Redis pub/sub**, NATS, or Kafka — where every WS/SSE server subscribes to the relevant channels and publishes inbound events. The broker fans out; each server pushes only to the sockets it locally owns. This is the standard "many WS servers behind one Redis" topology.

## Decision table

| | Short polling | Long polling | SSE | WebSocket |
|---|---|---|---|---|
| Direction | client pull | client pull (deferred) | server → client | full-duplex |
| Transport | HTTP req/resp | HTTP req/resp (held) | long-lived HTTP stream | upgraded TCP (101) |
| Payload | text/JSON | text/JSON | text only (UTF-8) | text **or** binary |
| Latency | ~poll interval | near real-time | real-time | real-time |
| Per-msg overhead | high (full headers) | medium | low | lowest |
| Reconnect | n/a | built into loop | **automatic + `Last-Event-ID`** | **DIY** |
| Proxy friendliness | best | best | good (HTTP/2!) | weakest (`wss://` helps) |
| Conn limit gotcha | none | none | ~6/domain on HTTP/1.1 | none |
| Best for | rare updates | fallback | feeds, notifications, LLM tokens | chat, games, collab |

The interview-grade summary: default to **SSE** for server→client feeds (auto-reconnect and resumability are gifts; just serve it over HTTP/2), reach for **WebSockets** only when you need low-latency *client→server*, keep **long polling** in your pocket as the universal fallback, and use **short polling** when "slightly stale" is genuinely acceptable. Naming the cheap option first is the signal that you've done this before.

**Try next:** work through how a slow WebSocket consumer creates unbounded server-side send buffers, and the flow-control strategies that prevent it, in the companion article on [backpressure and flow control](/articles/sys-patterns/2026-07-31-backpressure-flow-control).
