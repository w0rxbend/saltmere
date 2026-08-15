---
title: "Leader election: how a replicated workload picks exactly one active worker"
date: 2026-07-25
track: sys-patterns
summary: "Running three copies of a stateful worker for availability invites two of them to do the same job at once. Leader election picks one leader with a lease, and fencing tokens keep a paused leader from corrupting data after its lease has lapsed."
reading_time: 6
tags: [leader-election, kubernetes, lease, fencing]
sources:
  - title: "Burns, Designing Distributed Systems (2nd ed.) — replicated serving patterns"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "client-go leaderelection package (godoc, incl. fencing caveat)"
    url: "https://pkg.go.dev/k8s.io/client-go/tools/leaderelection"
  - title: "client-go leader-election example (main.go)"
    url: "https://github.com/kubernetes/client-go/blob/master/examples/leader-election/main.go"
  - title: "etcd clientv3/concurrency — sessions & mutex"
    url: "https://pkg.go.dev/go.etcd.io/etcd/client/v3/concurrency"
  - title: "Kleppmann — How to do distributed locking (fencing tokens)"
    url: "https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html"
---

**Gist.** Some workloads — a controller reconciling cluster state, a cron scheduler, a job draining a queue into a database — are not safe to run concurrently, yet still need several replicas so that failure of one does not stop the work. Leader election runs N replicas of which **exactly one holds a time-limited lease and does the work**, the rest idling as hot standbys until the lease lapses. The cost is that a lease bounds liveness, not safety: a leader stalled longer than its lease can wake up still believing it leads, so correctness requires a **fencing token checked by the protected resource**, not by the leader.

## A lease is a lock with a deadline

A plain lock in a database fails under crash: if the holder dies while holding the lock, the lock is held indefinitely and no replica can take over. A **lease** removes that failure by attaching an expiry. The holder must **renew before a time-to-live (TTL) elapses**. Renewal in time preserves leadership; a missed deadline — crash, network partition, or a long garbage-collection (GC) pause — lets the lease lapse and frees a standby to claim it.

Three durations parameterise the protocol, and their ratio is the tuning knob:

- **LeaseDuration** — how long a candidate observes no change to the lease record before attempting to take over.
- **RenewDeadline** — how long the current leader keeps retrying a refresh before giving up and stepping down.
- **RetryPeriod** — the interval between attempts.

The ordering that makes the protocol coherent is `RetryPeriod < RenewDeadline < LeaseDuration`: a leader gets several renewal attempts inside its own deadline, and it abandons leadership before any candidate is entitled to seize the lease. Failover latency observed by a client is therefore bounded below by roughly one `LeaseDuration` after the leader stops renewing.

## The Kubernetes implementation

Kubernetes ships a purpose-built application programming interface (API) object for this: `Lease` in the `coordination.k8s.io` group. The `client-go` library wraps the protocol around it, taking a `LeaseLock` and a set of callbacks and handling acquisition, renewal, and yielding.

```go
import (
    "k8s.io/client-go/tools/leaderelection"
    "k8s.io/client-go/tools/leaderelection/resourcelock"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

lock := &resourcelock.LeaseLock{
    LeaseMeta:  metav1.ObjectMeta{Name: "my-controller", Namespace: "default"},
    Client:     client.CoordinationV1(),
    LockConfig: resourcelock.ResourceLockConfig{Identity: id}, // unique per replica
}

leaderelection.RunOrDie(ctx, leaderelection.LeaderElectionConfig{
    Lock:            lock,
    ReleaseOnCancel: true,
    LeaseDuration:   60 * time.Second,
    RenewDeadline:   15 * time.Second,
    RetryPeriod:     5 * time.Second,
    Callbacks: leaderelection.LeaderCallbacks{
        OnStartedLeading: func(ctx context.Context) { run(ctx) },
        OnStoppedLeading: func()                    { os.Exit(0) }, // leadership lost: stop now
        OnNewLeader:      func(identity string)      { klog.Infof("leader: %s", identity) },
    },
})
```

The callback contract carries the weight. **All real work lives inside `OnStartedLeading`, whose context is cancelled when leadership ends**, and `OnStoppedLeading` fires the moment the library fails to renew within `RenewDeadline`; the process must cease acting immediately. Beneath the callbacks, every replica races to write its `Identity` and a fresh timestamp into the same `Lease` object, and **a compare-and-swap on the object's resource version admits only one writer per version**, which is what prevents two replicas from simultaneously recording themselves as holder. The current state is directly observable: `kubectl get lease my-controller -o yaml` shows `holderIdentity` and `renewTime` advancing.

Outside Kubernetes the shape is the same. etcd's `clientv3/concurrency` package provides a `Session` — a lease with a keepalive — and a `Mutex` built on it; Consul sessions occupy the same role. The requirement is a consistent store that can perform a conditional write.

## A lease is not fencing

The `client-go` documentation states the limit directly: *"This implementation does not guarantee that only one client is acting as a leader (a.k.a. fencing)."* Kleppmann's [distributed locking critique](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) explains the mechanism of the violation. A leader can pause — a stop-the-world GC, a hypervisor freeze — for longer than the lease. The lease expires, a standby legitimately acquires it, and the original leader resumes still believing it leads and issues a write. Two processes have now written, with no error raised anywhere.

> "if the GC pause lasts longer than the lease expiry period, and the client doesn't realise that it has expired, it may go ahead and make some unsafe change."

**Re-checking the lease immediately before writing does not close the hole**, because the pause can land between the check and the write. The remedy moves the safety check to the resource being protected: a **fencing token**. In Kleppmann's formulation a fencing token is a number that increases every time a client acquires the lock. The leader stamps every write with its token; **the storage service records the highest token it has accepted and rejects any write carrying a lower one**. The stale leader's write bounces on arrival, rather than being caught by the leader's own — untrustworthy — self-assessment. The token has to come from a counter that only ever increases across acquisitions — a `Lease` object's resource version is not a safe choice, since the Kubernetes API documents resource versions as opaque strings that clients must not treat as ordered — and **the guarantee exists only if the downstream store performs the comparison**. Leader election eliminates the common case of two concurrently active workers; fencing eliminates the rare, silent, data-corrupting one.

A partial mitigation available without downstream changes is `leaderelection.HealthzAdaptor`, which reports unhealthy when a process still holds the lease record but has failed to renew it, so that the platform can terminate it and shrink the window in which a stale leader can act. It shortens the window; it does not close it.

### Implementation sketch (Scala)

The load-bearing idea is the monotonic check at the resource, not the election loop. The store keeps the highest token it has accepted per protected key and refuses anything older.

```scala
type Fence = Long

final case class Rejected(key: String, presented: Fence, highest: Fence)

final class FencedStore[V]:
  // Single source of truth: the guarded value and the token that last wrote it.
  private val cells = java.util.concurrent.ConcurrentHashMap[String, (Fence, V)]()

  /** Applies the write only if `token` is at least as high as every token seen for `key`. */
  def write(key: String, token: Fence, value: V): Either[Rejected, V] =
    var outcome: Either[Rejected, V] = Right(value)
    cells.compute(
      key,
      (_, current) =>
        current match
          case null                              => (token, value)
          case (seen, held) if token < seen      =>
            outcome = Left(Rejected(key, token, seen)) // stale leader resumed after its lease lapsed
            (seen, held)
          case _                                 => (token, value)
    )
    outcome
```

`compute` runs the comparison and the update under the map's per-bin lock, so **the check and the write are atomic with respect to other writers** — the property that a leader-side check before a separate write cannot provide. A leader acquiring the lease obtains a strictly greater token from a counter maintained for that purpose, and passes it on every call.

## Pitfalls

- **Work started outside `OnStartedLeading` keeps running after leadership is lost.** Background goroutines, timers, or connections created at process start do not observe the cancelled leader context, so a demoted replica continues writing.
- **`OnStoppedLeading` that logs and returns leaves a second active worker.** The callback is the only signal that the lease was not renewed; returning from it without stopping work, or exiting, means the process acts without a lease.
- **`LeaseDuration` not greater than `RenewDeadline` is rejected outright.** `client-go` validates the ordering when the elector is constructed and returns an error rather than running with a configuration in which candidates become eligible to seize the lease before the incumbent has exhausted its own renewal attempts.
- **A long GC or hypervisor pause silently produces two leaders.** The paused leader neither renews nor observes the expiry, and resumes issuing writes that the store accepts because nothing downstream checks a token.
- **Treating leader election as sufficient for correctness.** The `client-go` documentation disclaims fencing; without a monotonic token enforced by the protected store, the pattern bounds only the frequency of concurrent writers, not their possibility.
- **Reusing a token that does not increase monotonically across acquisitions.** A token derived from a value that can repeat or reset lets a stale write compare as acceptable, defeating the rejection rule.
