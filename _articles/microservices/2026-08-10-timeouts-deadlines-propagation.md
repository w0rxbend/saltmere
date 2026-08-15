---
title: 'Timeouts and Deadline Propagation: Spending a Time Budget, Not a Guess'
date: 2026-08-10
track: microservices
summary: 'Every remote call needs a timeout, or one hung dependency drains a thread pool and takes a fleet down. A per-attempt timeout is not sufficient: an overall deadline, propagated down every hop, stops a downstream service from working on a request its caller has already abandoned.'
reading_time: 7
tags:
- timeouts
- deadlines
- resiliency
- grpc
- go
- context
- backpressure
- context-cancellation
- tail-latency
sources:
- title: gRPC — Deadlines guide
  url: https://grpc.io/docs/guides/deadlines/
- title: Amazon Builders' Library — Timeouts, retries, and backoff with jitter (Marc Brooker)
  url: https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter
- title: Go — context package documentation
  url: https://pkg.go.dev/context
- title: Microsoft Learn — Reliable gRPC services with deadlines and cancellation
  url: https://learn.microsoft.com/en-us/aspnet/core/grpc/deadlines-cancellation
- title: gRPC over HTTP/2 (PROTOCOL-HTTP2) — grpc-timeout
  url: https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md
- title: Google SRE Book — Addressing Cascading Failures
  url: https://sre.google/sre-book/addressing-cascading-failures/
- title: 'Go Blog — Go Concurrency Patterns: Context'
  url: https://go.dev/blog/context
---

**Gist.** A remote call without a timeout has unbounded resource-holding time, so a dependency that becomes slow rather than failing parks caller threads, connections and ephemeral ports until the caller's concurrency is exhausted. The remedy is two-layered: a per-attempt timeout that bounds one try, and an absolute **deadline** for the whole logical operation that is propagated to every downstream hop as remaining budget. The cost is a chosen rate of false timeouts — requests aborted although the dependency would have answered — traded for a hard bound on how long any single call can hold resources.

The [Amazon Builders' Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter) states the failure directly: when a request hangs, the client keeps holding "memory, threads, connections, [and] ephemeral ports," and "when a number of requests hold on to resources for a long time, the server can run out of those resources." The cascade proceeds without any process crashing; resources are never released.

Circuit breakers and retries are built on top of this layer. A breaker cannot observe a failure that never returns, and a retry policy cannot bound total work without a clock that spans attempts.

## A timeout bounds resource holding, not latency

A timeout does not make a call faster. It guarantees that a call **releases its resources within a bounded time regardless of the peer's behaviour**. Without one, achievable concurrency is capped by the worst-behaved dependency in the call graph. With a pool of 200 threads and a dependency that hangs for 30 seconds, the pool sustains roughly 200 / 30 ≈ 6–7 hung requests per second before it is fully occupied and healthy traffic queues behind dead calls.

The rule is therefore unconditional: **every remote call carries a timeout** — remote procedure call (RPC), database query, cache lookup, HTTP fetch. gRPC defaults to *no* deadline, so an unconfigured client will "wait forever for a response"; the bound must be set deliberately rather than inherited.

## Per-attempt timeout against overall deadline

A **per-attempt timeout** bounds a single try. An **overall deadline** bounds the entire logical operation, including retries and every downstream hop. The two are not interchangeable.

Consider a handler permitted 800 ms end-to-end. An 800 ms per-attempt timeout combined with two retries permits 800 + 800 + 800 = 2.4 s of elapsed time before the operation gives up — three times the budget promised to the caller. **Retries must fit inside the remaining deadline rather than restarting the clock.** The correct model gives the operation one wall-clock deadline and each attempt a per-try timeout equal to min(its own limit, time remaining on the deadline). When the deadline is spent, the operation terminates, whatever retry count remains unused. Retry mechanics are treated separately in [retries and backoff with jitter](/articles/microservices/2026-08-10-retries-backoff-jitter).

gRPC models the bound as a **deadline** — "a point in time past which a client is unwilling to wait" — rather than as a duration. An absolute instant remains meaningful across an arbitrary number of hops and retries, because it does not reset when a new attempt begins.

## Propagation carries remaining budget, not an instant

When service A calls B and B calls C, the deadline flows down the chain. If A abandoned the request 50 ms ago, work performed by C produces a result no caller will read.

Whether the deadline reaches the outbound call automatically depends on the implementation: where the inbound request carries a context or call object, passing that same value into downstream calls is what carries the budget forward, and an outbound call constructed with a fresh context starts an unbounded one. The mechanism, per the [gRPC deadlines guide](https://grpc.io/docs/guides/deadlines/), is that the absolute wall-clock instant is **not** placed on the wire. At each hop the deadline is converted back into a *timeout* — the remaining budget — by "deducting time already spent." The guide states that this shields the system from "clock skew issues" between machines whose clocks are not synchronised. Each service receives how much time is left, not the instant at which to stop, so **unsynchronised clocks cannot corrupt the budget**; only the elapsed-time measurement local to each hop can.

The observable consequence is a budget that shrinks monotonically as it descends:

```
Client sets deadline: 500ms
  -> API gateway receives ~495ms left (5ms network)  spends 20ms
     -> Order service receives ~470ms left            spends 15ms + a retry
        -> Inventory service receives ~430ms left     query takes 450ms -> DEADLINE_EXCEEDED
```

Inventory never completes; it is cancelled when the budget runs out, and the failure surfaces as `DEADLINE_EXCEEDED` rather than as a hang.

## The Go carrier: shrinking budget and cooperative cancellation

In Go the deadline *is* the [`context.Context`](https://pkg.go.dev/context), whose documented role is to carry "deadlines, cancellation signals, and other request-scoped values across API boundaries and between processes." A bounded context is created once and passed down every call, so every callee reads the same shrinking budget.

```go
// Handler enters with an overall budget for the whole operation.
func (s *Server) PlaceOrder(ctx context.Context, req *pb.OrderRequest) (*pb.OrderReply, error) {
	// Cap this operation at 500ms total. If the incoming ctx already
	// carries a tighter deadline (propagated from the caller), that one wins.
	ctx, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancel() // Releasing is mandatory: a leaked cancel func leaks goroutines.

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
		// Stop immediately if the overall deadline is already exceeded.
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
			return ctx.Err() // deadline hit mid-backoff: stop rather than retry into the red
		}
	}
	return errors.New("charge failed after retries")
}
```

Three invariants are visible here. First, `WithTimeout` derives from the parent, so an already-tighter inbound deadline is preserved: **the budget can only shrink, never extend**. Second, `defer cancel()` is required — the Go documentation warns that failing to call the returned CancelFunc leaks the child and its children until the parent is cancelled or the timer fires, and `go vet` checks for the omission. Third, **cancellation is cooperative**: every select and every retry loop observes `ctx.Done()`, so a client hang-up or an expired deadline unwinds the chain instead of leaving work in flight. On the gRPC server side the RPC itself is handled automatically — the framework "automatically cancels the RPC" when the deadline passes — but "the server application is responsible for stopping any activity it has spawned": the blocking query, the spawned goroutine, the outbound call.

### Implementation sketch (Scala)

The same invariant expressed without an ambient context: a deadline value threaded explicitly, each attempt bounded by the smaller of its own limit and the time remaining.

```scala
import scala.concurrent.duration.*
import scala.concurrent.{ExecutionContext, Future}

/** An absolute instant, expressed against a monotonic clock.
  * Named to avoid the `scala.concurrent.duration.Deadline` imported above. */
final case class OpDeadline(atNanos: Long):
  def remaining: FiniteDuration = (atNanos - System.nanoTime()).nanos
  def expired: Boolean = remaining <= Duration.Zero
  /** Derived deadlines may only shrink. */
  def shrinkTo(d: FiniteDuration): OpDeadline =
    OpDeadline(math.min(atNanos, System.nanoTime() + d.toNanos))

def retrying[A](dl: OpDeadline, perAttempt: FiniteDuration, attempts: Int)(
    call: FiniteDuration => Future[A]
)(using ExecutionContext): Future[A] =
  def loop(n: Int): Future[A] =
    if dl.expired then Future.failed(java.util.concurrent.TimeoutException())
    else
      // The wire timeout is the remaining budget, never the original duration.
      val budget = perAttempt.min(dl.remaining)
      call(budget).recoverWith:
        case e if n > 1 && retryable(e) && !dl.expired => loop(n - 1)
  loop(attempts)
```

`shrinkTo` encodes the monotonicity invariant; `perAttempt.min(dl.remaining)` encodes the rule that an attempt never outlives the operation.

## Selecting the value from the latency distribution

Timeout values follow from measurement. The Builders' Library method fixes an acceptable rate of false timeouts and sets the timeout at the downstream latency percentile that corresponds to it: tolerating a false-timeout rate of *r* means cutting off at the (1 − *r*) percentile of measured downstream latency. The trade is explicit: a small fraction of falsely aborted requests in exchange for a hard bound on resource-holding time. A timeout of 30 s chosen "to be safe" removes the bound the mechanism exists to provide; a timeout below the dependency's normal tail manufactures errors and additional retry load. The per-attempt timeout is derived from the dependency's measured high percentile; the overall deadline is derived from the service-level objective (SLO) the service itself promises its caller.

## Deadlines as a load signal

A deadline is also a load-shedding input. A server under load checks the deadline **before** starting expensive work: when the budget is already spent, returning `DEADLINE_EXCEEDED` immediately avoids queueing a doomed request, which is [load shedding](/articles/microservices/2026-07-31-rate-limiting-load-shedding-token-bucket) driven by the caller's own budget. Repeated budget exhaustion against one dependency is the failure signal a [circuit breaker](/articles/microservices/2026-07-24-circuit-breakers-resilience4j) trips on. Timeouts detect slowness, deadlines bound and propagate it, breakers and shedding contain it.

The distinction to retain: a timeout protects a single call, a deadline protects the whole operation, and propagation prevents downstream work on behalf of a caller that has already departed.

## Pitfalls

- **A per-attempt timeout equal to the end-to-end budget, combined with retries.** Symptom: the caller observes latency at a multiple of the promised bound. Cause: each attempt restarts the clock instead of drawing from a shared remaining budget.
- **An omitted `cancel()` in Go.** Symptom: goroutine count grows monotonically under normal traffic. Cause: per the `context` documentation, the child and its children are not released until the parent is cancelled or the timer fires.
- **A server that ignores cancellation after the framework cancels the RPC.** Symptom: `DEADLINE_EXCEEDED` is returned to clients while CPU and database load stay high. Cause: gRPC cancels the RPC, but the application owns stopping the activity it spawned — the blocking query keeps running.
- **Propagating an absolute wall-clock instant instead of remaining time.** Symptom: budgets that expand or vanish depending on which host handles the hop. Cause: clock skew between machines; gRPC converts the deadline back into a timeout at each hop precisely to avoid this.
- **An unconfigured gRPC client.** Symptom: a call that never returns while a dependency is slow. Cause: gRPC sets no deadline by default, so the client waits indefinitely.
- **A backoff sleep that is not raced against the deadline.** Symptom: the operation overruns its budget inside a wait rather than inside a call. Cause: the sleep is not selected against `ctx.Done()`, so expiry is observed only after the sleep completes.
- **A timeout chosen as a round number rather than from the latency distribution.** Symptom: either false timeouts on healthy traffic or no effective bound at all. Cause: no acceptable false-timeout rate was fixed and mapped onto a measured percentile.
