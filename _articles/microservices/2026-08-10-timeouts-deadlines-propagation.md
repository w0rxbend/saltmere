---
title: "Timeouts and Deadline Propagation: Spend a Time Budget, Not a Guess"
date: 2026-08-10
track: microservices
summary: "Every remote call needs a timeout, or one hung dependency drains your thread pool and takes the whole fleet down. But a per-attempt timeout isn't enough: you need an overall deadline, propagated down every hop, so a downstream service stops working on a request its caller already abandoned."
reading_time: 6
tags: [timeouts, deadlines, resiliency, grpc, go, context, backpressure]
sources:
  - title: "gRPC — Deadlines guide"
    url: "https://grpc.io/docs/guides/deadlines/"
  - title: "Amazon Builders' Library — Timeouts, retries, and backoff with jitter (Marc Brooker)"
    url: "https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter"
  - title: "Go — context package documentation"
    url: "https://pkg.go.dev/context"
  - title: "Microsoft Learn — Reliable gRPC services with deadlines and cancellation"
    url: "https://learn.microsoft.com/en-us/aspnet/core/grpc/deadlines-cancellation"
---

Ask an interviewer to name the cheapest resiliency bug you can ship and they'll often land here: a remote call with no timeout. It looks harmless in code review — the happy path works, latency is fine in staging. Then one afternoon a dependency gets slow instead of failing, and every thread that calls it parks, waiting forever. The [Amazon Builders' Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter) puts it plainly: when a request hangs, the client keeps holding "memory, threads, connections, [and] ephemeral ports," and "when a number of requests hold on to resources for a long time, the server can run out of those resources." That is how a single slow dependency cascades into a full outage — not by crashing, but by never letting go.

This is the under-appreciated staple. Circuit breakers and retries get the attention, but they're built on top of timeouts. Get timeouts and deadlines right and the rest of your resiliency stack has something to stand on.

## A timeout is a resource-protection tool, not a latency knob

The point of a timeout is not to make a call fast. It's to guarantee that a call *releases its resources* within a bounded time no matter what the other side does. Without one, your concurrency is capped by the worst-behaved dependency you talk to. A pool of 200 threads and a dependency that hangs for 30 seconds means you can absorb roughly 6-7 hung requests per second before the pool is gone and healthy traffic starts queueing behind the dead calls.

So the first rule is unconditional: **every remote call gets a timeout.** RPC, database query, cache lookup, HTTP fetch — no exceptions. gRPC actually defaults to *no* deadline, which means an un-configured client will "wait forever for a response," so this is a thing you have to do on purpose, not a default you inherit.

## Per-attempt timeout vs. the overall deadline

Here's the distinction interviewers probe. A **per-attempt timeout** bounds a single try. An **overall deadline** bounds the entire logical operation, including retries and multiple downstream hops.

They are not interchangeable. Say a handler is allowed 800 ms end-to-end. If you set a 800 ms per-attempt timeout *and* allow two retries, a slow dependency can burn 800 + 800 + 800 = 2.4 s before you give up — three times the budget you promised your own caller. Retries must fit *inside* the remaining deadline, not restart the clock. The right model is: the operation has one wall-clock deadline; each attempt gets a per-try timeout that is the smaller of (its own limit, time left on the deadline). When the deadline is spent, you stop — no more retries, no matter how many you had budgeted. (More on retry mechanics in [retries and backoff with jitter](/articles/microservices/2026-08-10-retries-backoff-jitter).)

gRPC leans into this by modeling the bound as a **deadline** — "a point in time past which a client is unwilling to wait" — rather than a duration. An absolute deadline is the natural thing to propagate, because it stays meaningful no matter how many hops or retries happen underneath it.

## Deadline propagation: don't work on an abandoned request

Now the part that separates a good answer from a great one. When service A calls B, and B calls C, the deadline should flow *down* the chain. If A gave up 50 ms ago, there is no reason for C to still be computing — that's pure wasted work, burning capacity on a result nobody will read.

gRPC does this automatically in Go and Java (C++ requires enabling it). The clever detail, straight from the [gRPC deadlines guide](https://grpc.io/docs/guides/deadlines/): it does **not** send the absolute wall-clock time across the wire. It converts the deadline back into a *timeout* — the remaining budget — at each hop, "deducting time already spent." This "shields your system from any clock skew issues" between machines whose clocks aren't perfectly synced. Each service receives "how much time is left," not "the instant to stop at," so unsynchronized clocks can't corrupt the budget.

The consequence is a budget that visibly shrinks as it descends:

```
Client sets deadline: 500ms
  -> API gateway receives ~495ms left (5ms network)  spends 20ms
     -> Order service receives ~470ms left            spends 15ms + a retry
        -> Inventory service receives ~430ms left     query takes 450ms -> DEADLINE_EXCEEDED
```

Inventory never finishes; it's cancelled the moment the budget runs out, and the failure bubbles up as `DEADLINE_EXCEEDED` rather than a hang.

## The Go example: budget shrinking and cancellation

In Go, the deadline *is* the [`context.Context`](https://pkg.go.dev/context). Its whole job is to carry "deadlines, cancellation signals, and other request-scoped values across API boundaries and between processes." You create a bounded context, pass it down every call, and everyone downstream reads the same shrinking budget.

```go
// Handler enters with an overall budget for the whole operation.
func (s *Server) PlaceOrder(ctx context.Context, req *pb.OrderRequest) (*pb.OrderReply, error) {
	// Cap this operation at 500ms total. If the incoming ctx already
	// carries a tighter deadline (propagated from our caller), that one wins.
	ctx, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancel() // ALWAYS release: leaking cancel funcs leaks goroutines.

	// Hop 1: reserve inventory. The gRPC client sends the *remaining*
	// budget as this call's timeout — not the original 500ms.
	if _, err := s.inventory.Reserve(ctx, &pb.ReserveRequest{Sku: req.Sku}); err != nil {
		return nil, err // DEADLINE_EXCEEDED here means the budget is already gone.
	}

	// Hop 2: charge payment. Same ctx, so it sees whatever time hop 1 left behind.
	// A retry loop here must respect ctx.Deadline(), not reset the clock:
	if err := s.chargeWithRetry(ctx, req); err != nil {
		return nil, err
	}
	return &pb.OrderReply{Status: "confirmed"}, nil
}

func (s *Server) chargeWithRetry(ctx context.Context, req *pb.OrderRequest) error {
	for attempt := 0; attempt < 3; attempt++ {
		// Bail immediately if the overall deadline is already blown.
		if err := ctx.Err(); err != nil {
			return err // context.DeadlineExceeded or context.Canceled
		}
		err := s.payment.Charge(ctx, req) // gRPC propagates remaining budget downstream
		if err == nil || !isRetryable(err) {
			return err
		}
		// Sleep, but never past the deadline — whichever fires first.
		select {
		case <-time.After(backoff(attempt)):
		case <-ctx.Done():
			return ctx.Err() // deadline hit mid-backoff: stop, don't retry into the red
		}
	}
	return errors.New("charge failed after retries")
}
```

Three things to point at in an interview. First, `WithTimeout` derives from the parent, so an already-tighter inbound deadline is preserved — you can only ever *shrink* the budget, never extend it. Second, `defer cancel()` is mandatory: the Go docs warn that "failing to call the CancelFunc leaks the child and its children until the parent is canceled," and `go vet` checks for it. Third, cancellation is cooperative — every select and every retry loop watches `ctx.Done()`, so when the client hangs up (or the deadline fires) the whole chain unwinds instead of grinding on. On the gRPC server side this is automatic: it "automatically cancels the RPC" when the deadline passes, but "the server application is responsible for stopping any activity it has spawned" — the blocking query, the goroutine, the outbound call.

## Picking the number: p99, not vibes

Timeout values should come from data, not a round number that "feels safe." The Builders' Library method: pick an acceptable rate of false timeouts — say 0.1% — then set the timeout at the corresponding downstream latency percentile (p99.9 in that example). You're explicitly trading a tiny fraction of falsely-timed-out requests for a hard bound on how long a call can pull resources. A too-generous timeout (30 s "to be safe") defeats the entire purpose; a too-tight one manufactures errors and retry load. Measure the dependency's real latency distribution, set the per-attempt timeout near its high percentile, and derive the overall deadline from what your *own* SLO promises the caller.

## Where deadlines meet load

Deadlines are also a load-shedding signal, which is why they pair naturally with the rest of the stack. A server drowning in work should check the deadline *before* starting expensive work: if the budget is already spent, return `DEADLINE_EXCEEDED` immediately instead of queueing a doomed request — that's [load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket) driven by the caller's own budget. And when a dependency is failing fast because the budget keeps running out, that's exactly the signal a [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) trips on. Timeouts detect the slowness; deadlines bound and propagate it; breakers and shedding contain it.

The one-line takeaway worth memorizing: a timeout protects a single call, a deadline protects the whole operation, and propagation makes sure nobody downstream keeps working for a caller who has already walked away.

**Try next:** wrap one un-timed client in your codebase in a `context.WithTimeout` derived from the inbound request, log `ctx.Err()` at each hop, and watch the budget shrink across a real trace — then set the value from the dependency's measured p99, not a guess.
