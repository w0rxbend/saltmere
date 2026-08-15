---
title: 'Real-Time to the Browser: Polling vs SSE vs WebSockets'
date: 2026-08-10
track: microservices
summary: How bytes reach a browser from a server without a page refresh. Short polling, long polling, Server-Sent Events and WebSockets compared by direction, transport, per-message overhead, reconnection semantics and proxy behaviour, with a decision table and minimal working code.
reading_time: 9
tags:
- realtime
- websocket
- sse
- http
- scaling
- server-sent-events
- long-polling
- system-design
sources:
- title: Using server-sent events (MDN Web Docs)
  url: https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
- title: EventSource (MDN Web Docs)
  url: https://developer.mozilla.org/en-US/docs/Web/API/EventSource
- title: Server-sent events (WHATWG HTML Standard, §9.2)
  url: https://html.spec.whatwg.org/multipage/server-sent-events.html
- title: RFC 6455 — The WebSocket Protocol
  url: https://www.rfc-editor.org/rfc/rfc6455.html
- title: The WebSockets API (MDN Web Docs)
  url: https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API
- title: RFC 6455 — The WebSocket Protocol
  url: https://datatracker.ietf.org/doc/html/rfc6455
- title: Slack Engineering — Migrating Millions of Concurrent Websockets to Envoy
  url: https://slack.engineering/migrating-millions-of-concurrent-websockets-to-envoy/
- title: Ably — What is HTTP Long Polling?
  url: https://ably.com/topic/long-polling
- title: MDN — WebTransport API
  url: https://developer.mozilla.org/en-US/docs/Web/API/WebTransport_API
---

**Gist.** HTTP is a request/response protocol in which the client speaks first, yet live feeds, chat and notifications require the server to deliver data the client did not ask for. Four mechanisms bridge that gap — short polling, long polling, Server-Sent Events (SSE) and WebSockets — by progressively converting per-request state into per-connection state. The cost is that the connection becomes long-lived and pinned to one server process, so reconnection, heartbeats, idle timeouts and cross-node fan-out become application concerns.

## Short polling

The client issues `GET /messages?since=...` on a timer. When nothing is new the server returns an empty result and the round trip has still been paid for.

- **Direction:** client-initiated pull, one exchange per request.
- **Transport:** ordinary HTTP request/response.
- **Overhead:** highest of the four. Every poll carries full headers and cookies, and most return nothing. **Staleness is bounded above by the poll interval**: a 5-second interval admits up to 5 seconds of delay.
- **Reconnection and back-pressure:** neither arises; each request is independent and there is no connection to drop.
- **Proxy behaviour:** unremarkable HTTP, understood by every intermediary in the path.

Short polling suits infrequent updates with bounded staleness, and is the baseline for the other three.

## Long polling

The client sends a request and the server **holds the response open** until an event occurs or a hold timeout fires; the client then issues the next request. Push is thereby simulated over request/response.

- **Direction:** client-initiated, but the response is deferred to the instant of a real event, so effective latency approaches that of a push channel.
- **Transport:** HTTP request/response with a held connection: one message per response, then a reconnect.
- **Overhead:** no empty responses, so it improves on short polling at low event rates. Every message still costs a fresh request with full headers, and **as the event rate rises the pattern degrades toward short polling**.
- **Reconnection and back-pressure:** the reconnect loop is the pattern, and back-pressure is intrinsic — the client re-arms only after processing the previous response.
- **Proxy behaviour:** normal HTTP, with one constraint: **intermediary read timeouts must exceed the server's hold window**, or the proxy severs the request before the event arrives.

Its remaining role is as a fallback: where SSE or WebSockets are blocked by an intermediary, a client that can also speak long polling still reaches the server.

## Server-Sent Events

SSE is a one-way server-to-client stream carried in a single long-lived HTTP response with `Content-Type: text/event-stream`. The server does not close the body; it keeps writing newline-delimited events. The browser-side interface is the built-in `EventSource`.

- **Direction:** **server to client only.** Upstream traffic uses ordinary HTTP requests.
- **Transport:** one long-lived HTTP/1.1 or HTTP/2 response. This is HTTP *streaming*, not a protocol upgrade; HTTP semantics hold throughout.
- **Overhead:** one connection amortizes all messages; each event costs a few bytes of `data:`, `event:` and `id:` framing plus the payload. **The payload is UTF-8 text only**; binary must be base64-encoded, inflating media transfers.
- **Reconnection and back-pressure:** **reconnection is automatic.** When the connection drops the browser reopens it. Each event may carry an `id:`; on reconnect the browser sends the last one in a `Last-Event-ID` request header, allowing the server to resume from that point, and a `retry:` field sets the reconnection delay in milliseconds. Resumability is part of the protocol rather than of the application.
- **Proxy behaviour:** good, with one structural limit. **On HTTP/1.1 a browser permits roughly six connections per domain**, and each `EventSource` consumes one, so several open tabs exhaust the pool; Chrome and Firefox have both marked this as not to be fixed. **Under HTTP/2 the limit effectively disappears**: streams are multiplexed over one TCP connection, with a maximum negotiated between the endpoints that commonly defaults to 100.

SSE suits feeds, notifications, live scores, log tailing, progress reporting and token-streaming model output. An endpoint with a heartbeat and resumable identifiers (Node/Express):

```js
app.get("/events", (req, res) => {
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  let id = Number(req.headers["last-event-id"] || 0);   // resume point after reconnect

  const timer = setInterval(() => {
    res.write(`id: ${++id}\nevent: price\n`);
    res.write(`data: ${JSON.stringify({ id, value: Math.random() })}\n\n`);
  }, 1000);
  // a comment line is a heartbeat that keeps proxies from idling the stream out
  const ka = setInterval(() => res.write(`: keep-alive\n\n`), 15000);

  req.on("close", () => { clearInterval(timer); clearInterval(ka); });
});
```

The client side carries no reconnection logic:

```js
const es = new EventSource("/events");
es.addEventListener("price", (e) => render(JSON.parse(e.data)));
```

## WebSockets

WebSocket, specified in RFC 6455, provides **full-duplex** communication over a single persistent TCP connection. It begins as HTTP: the client sends an `Upgrade: websocket` and `Connection: Upgrade` request carrying `Sec-WebSocket-Key`, the server answers **101 Switching Protocols** with the derived `Sec-WebSocket-Accept`, and the connection then speaks WebSocket framing rather than HTTP.

- **Direction:** **bidirectional** — the only mechanism in which the client pushes upstream at the latency the server pushes downstream.
- **Transport:** a persistent upgraded TCP connection; after the handshake there are no per-request headers, status codes or HTTP caching. Frames carry **text or binary** natively, and **client-to-server frames are masked, server-to-client frames are not**.
- **Overhead:** lowest per message — a small frame header, with no headers or cookies retransmitted, which favours frequent small messages.
- **Reconnection and back-pressure:** **both are application responsibilities.** The protocol supplies ping and pong control frames, but heartbeat cadence, reconnect with backoff and replay after a drop are application code; there is no `Last-Event-ID` equivalent. TCP provides transport-level flow control, but a slow consumer accumulating an unbounded server-side send buffer is an application problem — see the companion note on [backpressure and flow control](/articles/sys-patterns/2026-07-31-backpressure-flow-control).
- **Proxy behaviour:** the weakest of the four. Some older proxies and corporate firewalls do not handle the upgrade and either break or buffer the connection; `wss://` fares better, because an intermediary cannot inspect or rewrite an encrypted stream. Hence the long-polling fallback.

WebSockets are warranted where a low-latency upstream channel is required: chat, multiplayer games, collaborative editing, trading.

## Scaling the stateful mechanisms

Short and long polling scale as any stateless HTTP endpoint does. SSE and WebSockets differ because the connection is **long-lived and pinned to one server process**, imposing two requirements.

1. **Sticky routing.** A client's connection must remain on the process holding it; the load balancer routes per connection rather than per request.
2. **A publish/subscribe backplane.** A message for a user attached to a different node cannot be delivered locally. A broker — Redis publish/subscribe, NATS or Kafka — sits between: every gateway subscribes to the relevant channels and pushes only to the sockets it owns locally. Sticky routing then matters for in-flight session state rather than for correctness.

## The cost of a million connections

Persistent connections move cost from per message to per connection *state*: a file descriptor, kernel socket buffers, a TLS session, a heartbeat timer and a session object per client. Every load balancer in the path holds equivalent state, and **each load-balancer-to-backend pair draws from a roughly 64K ephemeral port space**. Deployment is the difficult case: restarting a gateway drops every connection it holds, and simultaneous reconnection is a thundering herd aimed at the authentication tier. Slack's [Envoy migration](https://slack.engineering/migrating-millions-of-concurrent-websockets-to-envoy/) account concerns draining millions of WebSockets slowly and surviving mass-reconnect storms.

Idle timeouts terminate quiet connections: **AWS Application Load Balancer defaults to a 60-second idle timeout, and nginx applies `proxy_read_timeout` similarly**. WebSockets therefore require ping/pong traffic inside that window, and SSE periodic `: keepalive` comment lines plus buffering disabled (`X-Accel-Buffering: no` for nginx), otherwise events accumulate in a proxy buffer.

Backfill after reconnect is the sharpest asymmetry. SSE supplies `Last-Event-ID`; WebSocket supplies nothing, so the application implements a resume token — a per-session monotonic sequence number echoed on reconnect, from which the server replays a short retained buffer.

### Implementation sketch (Scala)

The resume contract reduced to its invariant: **a bounded ring keyed by a monotonic sequence, plus an explicit "too old, resynchronize" answer** when the requested point has been evicted.

```scala
final case class Event(seq: Long, payload: String)

enum Resume:
  case Replay(events: Vector[Event])
  case Resync                       // requested seq evicted; client must refetch state

/** Per-session buffer retaining at most `capacity` most-recent events. */
final class ResumeBuffer(capacity: Int):
  private var next: Long = 0L
  private var ring: Vector[Event] = Vector.empty

  def append(payload: String): Event =
    val e = Event(next, payload)
    next += 1
    ring = (ring :+ e).takeRight(capacity)
    e

  /** `lastSeen` is the highest seq the client acknowledges having processed. */
  def since(lastSeen: Option[Long]): Resume = lastSeen match
    case None => Resume.Resync                       // fresh session: no baseline
    case Some(s) if s + 1 == next => Resume.Replay(Vector.empty)
    case Some(s) =>
      ring.headOption match
        // the oldest retained event is already past the client's cursor
        case Some(oldest) if oldest.seq > s + 1 => Resume.Resync
        case _ => Resume.Replay(ring.filter(_.seq > s))
```

`Resync` is returned rather than a silently truncated replay: a gap delivered as if contiguous leaves the client permanently divergent with no signal that it occurred.

## Decision table

| | Short polling | Long polling | SSE | WebSocket |
|---|---|---|---|---|
| Direction | client pull | client pull (deferred) | server → client | full-duplex |
| Transport | HTTP req/resp | HTTP req/resp (held) | long-lived HTTP stream | upgraded TCP (101) |
| Payload | text/JSON | text/JSON | text only (UTF-8) | text **or** binary |
| Latency | ~poll interval | near real-time | real-time | real-time |
| Per-msg overhead | high (full headers) | medium | low | lowest |
| Reconnect | n/a | built into loop | **automatic + `Last-Event-ID`** | **application-implemented** |
| Proxy friendliness | best | best | good (HTTP/2) | weakest (`wss://` helps) |
| Conn limit gotcha | none | none | ~6/domain on HTTP/1.1 | none |
| Best for | rare updates | fallback | feeds, notifications, token streams | chat, games, collaboration |

The resulting ordering: SSE for server-to-client feeds over HTTP/2, since reconnection and resumability are protocol-provided; WebSockets where low-latency client-to-server traffic is required; long polling as the universal fallback; short polling where bounded staleness is acceptable.

## Pitfalls

- **Several SSE tabs on one domain stop receiving events.** Each `EventSource` occupies one of the ~6 HTTP/1.1 connections per domain; once the pool is exhausted, further streams never open.
- **An SSE stream produces nothing until much data has accumulated.** A reverse proxy is buffering the response body; nginx requires `X-Accel-Buffering: no`.
- **Connections die every 60 seconds with no application error.** The load balancer's idle timeout — 60 seconds by default on AWS ALB — elapsed without traffic; ping/pong or `: keepalive` lines must fire inside that window.
- **Long polling returns errors under low event rates.** The intermediary's read timeout is shorter than the server's hold window, so the proxy severs the request before an event arrives.
- **A WebSocket client silently misses messages after a reconnect.** No `Last-Event-ID` equivalent exists; without a sequence number and replay buffer, everything sent during the disconnection is lost.
- **A gateway restart saturates the authentication tier.** Every connection it held drops at once and all clients reconnect simultaneously.
- **Server memory grows while a client stalls.** A slow WebSocket consumer does not stop the producer; the send buffer grows unbounded unless the application applies back-pressure.
