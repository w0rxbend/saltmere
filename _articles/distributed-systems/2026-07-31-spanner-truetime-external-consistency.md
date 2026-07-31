---
title: "TrueTime and commit-wait: how Spanner buys external consistency with an interval"
date: 2026-07-31
track: distributed-systems
summary: "Spanner gives you linearizability across a globally-distributed database without a global clock. The trick is to make time an interval, not a point: TrueTime returns [earliest, latest] with a bounded uncertainty, and Spanner deliberately waits out that uncertainty before releasing a transaction's locks. This article explains why that one wait is what makes timestamps respect real-time order."
reading_time: 6
tags: [spanner, truetime, external-consistency, linearizability, clocks, transactions]
sources:
  - title: "Corbett et al. — Spanner: Google's Globally-Distributed Database (OSDI 2012)"
    url: "https://www.usenix.org/system/files/conference/osdi12/osdi12-final-16.pdf"
  - title: "Spanner: TrueTime and external consistency (Google Cloud Documentation)"
    url: "https://cloud.google.com/spanner/docs/true-time-external-consistency"
  - title: "Strict serializability and external consistency in Spanner (Google Cloud Blog)"
    url: "https://cloud.google.com/blog/products/databases/strict-serializability-and-external-consistency-in-spanner"
---

Most distributed databases give you a consistency model with an asterisk: transactions are serializable, but the *order* the system picks need not match the order you actually observed. If you commit T1, phone a colleague, and they commit T2, a merely-serializable system is allowed to order T2 before T1. **External consistency** (a.k.a. strict serializability, or linearizability applied to whole transactions) forbids that: if T1 finishes before T2 starts in real time, every reader sees T1's timestamp as smaller. Spanner delivers this across datacenters on different continents — and the surprising part is it does so without a synchronized global clock. It uses one that admits it's wrong.

## Time as an interval, not an instant

A normal clock hands you a timestamp and lets you believe it. **TrueTime** refuses to. Its core call, `TT.now()`, returns a *TTinterval* `[earliest, latest]` that is *guaranteed* to contain the true absolute time of the call. The half-width of that interval is the uncertainty **ε** (epsilon).

Where does ε come from? Every datacenter runs time-master servers backed by two independent sources: GPS receivers, and **Armageddon masters** with atomic clocks whose failure modes are uncorrelated with GPS (a spoofed or down GPS constellation won't take out both). Machines poll masters, and between polls their local oscillator drift widens ε. The OSDI 2012 paper reports ε as a sawtooth that rises with drift and snaps back on each poll — in production it stays generally under 10 ms, averaging around 4 ms, and ranging roughly 1–7 ms across a poll interval.

Two convenience predicates fall out of the interval:

- `TT.after(t)` — is `t` definitely in the past? True when `t < TT.now().earliest`.
- `TT.before(t)` — is `t` definitely in the future? True when `t > TT.now().latest`.

## The two rules that force real-time order

Spanner assigns each read-write transaction a single commit timestamp `s`, chosen by the coordinator leader. Two rules do all the work:

**Start rule.** Pick `s = TT.now().latest`, evaluated *after* the commit request arrives. This guarantees `s` is at or beyond the true time at which the commit began.

**Commit-wait rule.** Do *not* release the transaction's locks — do not let anyone observe the commit — until `TT.after(s)` is true. That is, wait until `s` is unambiguously in the past according to *every* clock's worst case.

Put them together and the external-consistency invariant is forced. Say T1 commits before T2 begins in real time. T1 held its locks until `TT.after(s1)` was true, so real time had already passed `s1` before T1 was visible. T2 then started, and its Start rule sets `s2 = TT.now().latest ≥` the true time of its start `> s1`. Therefore `s1 < s2`, always, in the real-time order — exactly what external consistency demands. Commit-wait is the price: you pay one ε of latency so that no two non-overlapping transactions can ever get timestamps that lie about their order.

## What commit-wait looks like

The mechanism is almost anticlimactic — a spin until the uncertainty has elapsed:

```python
def truetime_now():
    """Returns (earliest, latest); the true instant lies inside."""
    center = clock.read()          # local best estimate
    eps = current_uncertainty()    # half-width, ~1-7 ms in prod
    return (center - eps, center + eps)

def after(t):
    earliest, _ = truetime_now()
    return t < earliest            # t is definitely in the past

def commit_transaction(txn):
    txn.prepare()                  # 2PC prepare across participants
    # Start rule: timestamp no earlier than 'now'
    _, latest = truetime_now()
    s = latest
    txn.assign_timestamp(s)

    # Commit-wait: block until s is unambiguously past for everyone.
    while not after(s):
        sleep(0.0005)              # spin out the remaining uncertainty

    txn.release_locks()            # only now is the commit observable
    return s
```

The expected wait is about 2ε (you set `s` at the top of the interval, then wait for the bottom of a future interval to pass it). That is why keeping ε small is not a nicety but the whole ballgame: shrink ε and you shrink both write latency and the read-staleness that snapshot reads inherit. Google's investment in GPS and atomic-clock hardware exists to keep that number down — the algorithm is trivial once the clock is honest about its error.

A nice consequence: reads need no locks and no commit-wait. A read at timestamp `t_read` just waits (via `TT.after`) until `t_read` is safely in the past at the replica, then returns a consistent snapshot — enabling lock-free, globally-consistent reads at any replica.

**Try next:** model it. Write a simulator with N nodes whose clocks each carry a random offset within ε; run transactions with and without the commit-wait loop, and log `(real_start, real_commit, assigned_ts)` for each. Sort by timestamp and check for any pair where T1 committed before T2 started yet `ts(T1) > ts(T2)` — you'll see violations vanish exactly when you switch commit-wait on.
