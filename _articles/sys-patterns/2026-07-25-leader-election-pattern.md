---
title: "Leader election: how a replicated workload picks exactly one boss"
date: 2026-07-25
track: sys-patterns
summary: "Run three copies of a stateful worker for availability and you invite two of them to do the same job at once. Leader election picks one leader with a lease, and fencing tokens keep a zombie leader from corrupting your data."
reading_time: 5
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

You replicate a stateless web server three ways and load balance across all of them — more copies, more throughput, done. But some workloads can't run concurrently: a controller that reconciles cluster state, a cron scheduler, a job that drains a queue and writes to a database. Run three of *those* and they trample each other. You still want three copies for availability — you just want exactly **one** doing the work at a time, and instant failover when it dies. That's the leader election pattern from Burns' replicated serving chapter: N replicas, one active leader, the rest hot standbys.

## A lease is a lock with a deadline

The naive answer — "grab a lock in a database" — has a fatal flaw: if the lock holder crashes while holding it, the lock is held forever and no one can take over. The fix is a **lease**: a lock that expires. The leader must keep *renewing* it before a TTL elapses. Renew in time and you stay leader; miss the deadline (crash, network partition, long GC pause) and the lease lapses, freeing a standby to claim it.

Three durations define the whole dance, and their ratio is the tuning knob:

- **LeaseDuration** — how long a candidate waits, seeing no change, before trying to take over.
- **RenewDeadline** — how long the current leader keeps retrying a refresh before giving up and stepping down.
- **RetryPeriod** — the gap between attempts.

## Kubernetes does this for you with Lease objects

Every Kubernetes cluster ships a purpose-built API object — `Lease` in the `coordination.k8s.io` group — and `client-go` wraps the whole protocol. You hand it a `LeaseLock` and callbacks; it handles acquiring, renewing, and yielding.

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
        OnStartedLeading: func(ctx context.Context) { run(ctx) }, // do the work here
        OnStoppedLeading: func()                    { os.Exit(0) }, // we lost it: stop NOW
        OnNewLeader:      func(identity string)      { klog.Infof("leader: %s", identity) },
    },
})
```

The contract is the important part. All your real work lives inside `OnStartedLeading`; the moment the library can't renew, it fires `OnStoppedLeading` and you must stop immediately. Under the hood every replica is racing to write its `Identity` and a fresh timestamp into the same `Lease` object; a compare-and-swap on the object's resource version means only one write wins. Watch it happen with `kubectl get lease my-controller -o yaml` — you'll see `holderIdentity` and `renewTime` tick.

If you're not on Kubernetes, the shape is identical elsewhere. etcd's `clientv3/concurrency` package gives you a `Session` (a lease with a keepalive) and a `Mutex` built on it; Consul sessions do the same. Pick whichever consistent store you already run.

## The catch: a lease is not fencing

Here's the trap, and `client-go`'s own docs say it plainly: *"This implementation does not guarantee that only one client is acting as a leader (a.k.a. fencing)."* Martin Kleppmann's [distributed locking critique](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html) shows why. Your leader can pause — a stop-the-world GC, a hypervisor freeze — for longer than the lease. The lease expires, a standby takes over, and then the old leader wakes up still *believing* it's the leader and issues a write. Now two leaders wrote. Split-brain, silently.

> "if the GC pause lasts longer than the lease expiry period, and the client doesn't realise that it has expired, it may go ahead and make some unsafe change."

Checking the lease right before writing doesn't save you — the pause can land between the check and the write. The real fix pushes safety down to the resource being protected: a **fencing token**. As Kleppmann puts it, *"a fencing token is simply a number that increases every time a client acquires the lock."* The leader stamps every write with its token; the storage service remembers the highest token it has seen and **rejects any write carrying a lower one**. The zombie leader's write arrives with a stale token and bounces. In Kubernetes terms, the `Lease` object's monotonic resource version (or a counter you keep) is that token — but only if your downstream store actually checks it. Leader election prevents the *common* case of two active workers; fencing prevents the *rare, catastrophic* one.

At minimum, wire up `leaderelection.HealthzAdaptor` so a leader that owns the lease but has failed to renew it reports unhealthy and gets killed, shrinking the window a zombie can act in.

**Try next:** deploy the client-go example with two replicas, then `kubectl delete pod` the leader and watch the standby's `OnStartedLeading` fire within one `LeaseDuration`. Then send SIGSTOP to a leader for longer than the lease and confirm — via the `Lease`'s `renewTime` — that a second replica took over while the first was frozen. That frozen process is your zombie; now you understand why fencing exists.
