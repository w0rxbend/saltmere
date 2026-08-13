---
title: "Deadline propagation: one budget for the whole call graph, not a timeout per hop"
date: 2026-08-13
track: sys-patterns
summary: "Per-hop timeouts compose into worst cases nobody chose: retries multiply them and servers burn CPU on requests the caller abandoned seconds ago. The fix is a single absolute deadline that travels with the request — gRPC's grpc-timeout header and Go's context chain do it for you."
reading_time: 5
tags: [timeouts, deadlines, grpc, context-cancellation, tail-latency]
sources:
  - title: "gRPC docs — Deadlines"
    url: "https://grpc.io/docs/guides/deadlines/"
  - title: "gRPC over HTTP/2 (PROTOCOL-HTTP2) — grpc-timeout"
    url: "https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md"
  - title: "Google SRE Book — Addressing Cascading Failures"
    url: "https://sre.google/sre-book/addressing-cascading-failures/"
  - title: "Go Blog — Go Concurrency Patterns: Context"
    url: "https://go.dev/blog/context"
  - title: "Marc Brooker (AWS) — Timeouts, retries, and backoff with jitter"
    url: "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/"
---

Give every service its own 2-second timeout and you have not built a 2-second system. A calls B calls C; B retries C once, A retries B once. Worst case at A: 2 s × 2 attempts at B, each hiding 2 s × 2 attempts at C — the user waits 8 s for an endpoint everyone "capped" at 2. Meanwhile the opposite failure runs concurrently: A gave up at 2 s, but C is still grinding through its query, doing work whose result nobody will read. The SRE book calls this out as a [cascading-failure ingredient](https://sre.google/sre-book/addressing-cascading-failures/): servers "consume resources on requests that will never be used," and under overload that wasted work is exactly what keeps you overloaded.

Per-hop timeouts compose *multiplicatively* with retries and *ignorantly* with abandonment. What you want is one **deadline** — an absolute point in time chosen at the edge — that every hop inherits, decrements, and obeys.

## How gRPC and Go actually propagate it

A gRPC client that sets a deadline causes the runtime to emit a [`grpc-timeout` header](https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md) — a value of at most 8 digits plus a unit (`H`/`M`/`S`/`m`/`u`/`n`), e.g. `grpc-timeout: 950m`. Two details matter. First, it's a *relative* timeout recomputed at each hop from the absolute deadline, with elapsed time already deducted — which, per the [gRPC deadline guide](https://grpc.io/docs/guides/deadlines/), "shields your system from any clock skew issues" between machines. Second, gRPC sets **no default deadline**: omit it and a client will "wait for a response effectively forever." Always set one.

In Go the carrier is [`context.Context`](https://go.dev/blog/context): the server runtime materializes the incoming `grpc-timeout` as a context deadline, and every outgoing call made with that same `ctx` re-emits the *remaining* time. Cancellation flows the same chain — when the edge client hangs up or the deadline fires, `ctx.Done()` closes in every goroutine downstream, in every service, and each can stop working. The one server-side catch: gRPC cancels the *RPC*, but "the server application is responsible for stopping any activity it has spawned" — you must actually check the context.

```go
// Edge client: ONE deadline for the whole tree. Wire: grpc-timeout: 1S
ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
defer cancel()
resp, err := gw.PlaceOrder(ctx, req)

// Gateway handler: ctx arrives carrying the REMAINING deadline.
func (s *srv) PlaceOrder(ctx context.Context, req *pb.Req) (*pb.Resp, error) {
    // Fail fast if there isn't enough budget left to plausibly succeed.
    if d, ok := ctx.Deadline(); ok && time.Until(d) < 100*time.Millisecond {
        return nil, status.Error(codes.DeadlineExceeded, "insufficient budget")
    }
    // Pass ctx down UNTOUCHED: this call inherits whatever time remains,
    // and gRPC rewrites grpc-timeout with elapsed time deducted.
    inv, err := s.inventory.Reserve(ctx, req.Items)
    if err != nil {
        return nil, err
    }
    if ctx.Err() != nil { // caller gone or out of time: don't do the write
        return nil, status.FromContextError(ctx.Err()).Err()
    }
    return s.commit(ctx, inv)
}
```

The load-bearing habit is passing the *same* `ctx` down. The moment someone writes `context.Background()` inside a handler, the chain is severed: that subtree becomes unkillable and will happily finish work for a caller that timed out minutes ago.

## Budgeting a 3-hop call

Treat the edge deadline as a budget each hop draws down. Reserve your own p99 work plus response marshaling, pass the remainder, and cap what you pass so a slow upstream can't spend your entire budget in one place.

| Stage | Budget on entry | Reserved locally (p99 work + marshal) | Sent downstream (`grpc-timeout`) |
|---|---|---|---|
| Edge client | 1000 ms | — | `1S` |
| API gateway | ~995 ms | 45 ms (authn, routing) | `950m` |
| Order service | ~945 ms | 145 ms (validation, commit, retry headroom) | `800m` |
| Inventory service | ~795 ms | 795 ms (DB + its own children) | — |

Two rules fall out. **Retries must fit the remaining budget, not restart it.** A retry at the order service is only legal if `time.Until(deadline)` still exceeds the attempt's p99 plus backoff; otherwise skip it and return `DEADLINE_EXCEEDED` now. (Backoff, jitter, and retry-storm mechanics are covered in [timeouts, retries, and bulkheads](/articles/microservices/2026-07-26-timeouts-retries-bulkheads) — the addition here is that every attempt shares one clock.) **Check the budget before starting expensive work, not just after.** The `< 100ms` guard above is deadline propagation's payoff: under overload, requests that would time out anyway get rejected in microseconds, shedding load instead of amplifying it.

## Tail behavior and defaults

Deadlines shape the tail. Without propagation your p99.9 is the sum of worst cases per hop; with it, the edge deadline is a hard ceiling — latency above it converts to fast, explicit errors, which are cheap to retry or degrade around, unlike mystery 8-second successes. Deadlines also compose with [request hedging](/articles/sys-patterns/2026-07-30-request-hedging-tail-latency): hedged attempts share the budget, and cancellation propagation is what reclaims the loser's work.

Recommended defaults, condensed from the [gRPC guide](https://grpc.io/docs/guides/deadlines/), the SRE book, and [Brooker's timeout rules](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/):

- Always set a deadline at the edge; never rely on a framework default that doesn't exist.
- Derive per-hop reservations from measured p99, not guesses; revisit when the p99 moves.
- Set deadlines end-users tolerate (hundreds of ms to a few s), not "safe" 60 s values that hold worker threads hostage during incidents.
- Propagate `ctx` untouched by default; *narrow* it (`context.WithTimeout(ctx, cap)`) only to stop one dependency from monopolizing the budget.
- On `DEADLINE_EXCEEDED` from downstream, don't retry unless real budget remains — return your own deadline error upward.

**Try next:** build a 3-service gRPC chain in Go, set a 1 s edge deadline, and add a `time.Sleep(2 * time.Second)` in the last hop — then log `ctx.Err()` at each service and watch the cancellation arrive everywhere at the 1 s mark, and confirm with a debug print of the `grpc-timeout` header that each hop received a smaller budget than the one before.
