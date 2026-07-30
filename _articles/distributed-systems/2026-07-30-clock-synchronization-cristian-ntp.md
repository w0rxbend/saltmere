---
title: "Syncing Physical Clocks: Cristian, Berkeley, and How NTP Actually Works"
date: 2026-07-30
track: distributed-systems
summary: "Wall clocks drift, so you cannot read a remote machine's time — you can only estimate it across a network with unknown latency. This walks through Cristian's RTT/2 correction, the Berkeley averaging algorithm, and the four-timestamp offset/delay math NTP runs in production, with runnable code and the chrony commands to inspect real offsets."
reading_time: 6
tags: [clock-synchronization, physical-clocks, ntp, cristian-algorithm, berkeley-algorithm, chrony]
sources:
  - title: "Probabilistic clock synchronization (Distributed Computing 3:146–158, 1989) — Flaviu Cristian"
    url: "https://doi.org/10.1007/BF01784024"
  - title: "The Accuracy of the Clock Synchronization Achieved by TEMPO in Berkeley UNIX 4.3BSD (IEEE TSE, 1989) — Gusella & Zatti"
    url: "https://dblp.org/rec/journals/tse/GusellaZ89.html"
  - title: "RFC 5905 — Network Time Protocol Version 4: Protocol and Algorithms Specification (June 2010)"
    url: "https://www.rfc-editor.org/rfc/rfc5905.html"
  - title: "chrony — Comparison of NTP implementations (accuracy figures)"
    url: "https://chrony-project.org/comparison.html"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
---

Every quartz oscillator runs at a slightly wrong frequency, so every machine's clock **drifts** — typically tens of parts per million, a few seconds a day if left alone. That is fine until you correlate logs across hosts, expire TLS certs, or enforce a lease. You cannot fix drift by reading a reference clock over the network, because the read itself takes an unknown, asymmetric amount of time. Physical-clock synchronization is the art of estimating that error and correcting for it. Note this is a *different* problem from Lamport/vector clocks (covered elsewhere here): those give you causal *order* without real time; here we want the actual wall-clock value, as close to UTC as the network allows.

## Cristian's algorithm: correct for the round trip

The core trick, from Flaviu Cristian's 1989 *Probabilistic clock synchronization*, is to measure the round-trip and assume the reply spent half of it in flight. Client stamps `T0` before the request, server replies with its time `T_server`, client stamps `T1` on receipt:

```
RTT   = T1 - T0
t_est = T_server + RTT / 2
```

You set your clock to `t_est`. Why `RTT/2`? Because the request and reply legs are assumed symmetric, so the server's timestamp was generated roughly `RTT/2` ago. The genius is the **bounded accuracy**: if `min` is the smallest possible one-way transit time, the server reading must have happened somewhere in the window `[T0 + min, T1 - min]`, so the true time now lies within

```
±(RTT/2 - min)
```

of your estimate. A smaller RTT is a tighter bound, which is why Cristian's probabilistic refinement is to fire *many* requests and keep only the response with the **lowest RTT** — the sample least polluted by queueing. That "take the minimum-delay sample" idea survives directly into NTP's filtering.

```python
import time

def cristian_sync(query_server):
    best = None                     # (rtt, offset)
    for _ in range(8):              # probe repeatedly, keep the cleanest
        t0 = time.time()
        t_server = query_server()   # remote UTC seconds
        t1 = time.time()
        rtt = t1 - t0
        offset = t_server + rtt / 2 - t1   # how far we are behind/ahead
        if best is None or rtt < best[0]:
            best = (rtt, offset)
    rtt, offset = best
    return offset, rtt / 2          # correction, and error bound (minus min)
```

The one caveat Cristian's model bakes in: it assumes the two legs are symmetric. Asymmetric routing (a fast path out, a congested path back) skews the estimate by half the asymmetry, and no amount of sampling detects it. This is the dominant error source on the real internet.

## The Berkeley algorithm: agree internally, no UTC needed

Cristian's model needs an authoritative time server. Gusella and Zatti's 1989 Berkeley algorithm (the `TEMPO` daemon in 4.3BSD) solves the opposite case: a cluster with *no* reliable external reference that just needs to agree with **each other**. It is master-driven and averaging:

1. A master polls every node for its clock (using Cristian-style round-trip correction to account for message delay).
2. The master **averages** the readings — after discarding outliers too far from the rest, so one wildly wrong clock or Byzantine node can't drag the group.
3. It computes each node's *offset from that average* and sends back a **relative adjustment** ("slow down by 40 ms"), never an absolute time.

Sending adjustments rather than absolute times matters: nodes never jump their clocks backward, they slew toward the agreed value. The master is itself just a node; if it dies, an election picks another. Berkeley gives you tight *internal* synchronization even when the whole cluster is offline from the world.

## NTP: Cristian, hardened for the internet

NTP (RFC 5905, NTPv4, June 2010) is Cristian's idea with four timestamps instead of two, so it cancels the server's own processing time. `T1` = client send, `T2` = server receive, `T3` = server send, `T4` = client receive:

```
offset θ = ½·[(T2 - T1) + (T3 - T4)]
delay  δ = (T4 - T1) - (T3 - T2)
```

`δ` subtracts the server's think-time `(T3 - T2)` from the total elapsed, leaving pure network round-trip; `θ` is the mean of the two one-way offset estimates. NTP collects many `(θ, δ)` pairs and, echoing Cristian, its clock filter prefers the samples with **lowest delay**. Servers form a **stratum** hierarchy — stratum 0 is a reference (GPS, atomic), stratum 1 syncs directly to it, and each level down adds one — and a client cross-checks several servers, discarding "falsetickers" before disciplining its clock by slewing the OS tick rate rather than stepping.

Real numbers: over the public internet expect single-digit-to-tens of milliseconds; on a LAN, sub-millisecond; with a local reference and a good implementation, tens of microseconds. On Linux, `chrony` generally beats classic `ntpd` — its own comparison shows ~109 µs vs ~256 µs under 100 µs of network jitter on a permanent link, and it stays useful on intermittent connections where `ntpd` degrades badly, because it models clock rate and never has to step.

Inspect your actual offset:

```console
$ chronyc tracking
Reference ID    : A29FC87B (time.cloudflare.com)
Stratum         : 3
System time     : 0.000023019 seconds slow of NTP time
RMS offset      : 0.000041991 seconds
Root delay      : 0.02216 seconds
Frequency       : 12.482 ppm slow
```

`System time` is your current offset (~23 µs here); `Frequency` is the measured drift chrony is compensating for — proof your quartz was never running at exactly the right rate. `chronyc sources -v` shows per-server delay/offset, the raw material of the formulas above.

**Try next:** Run `chronyc tracking` and `chronyc sourcestats` on any Linux box, note the `Frequency` (ppm) value, then compute how many seconds/day your bare oscillator would drift without correction (`ppm × 86400 / 1e6`). Then implement the `cristian_sync` loop against a public server (send an NTP mode-3 packet, or just hit an HTTP `Date` header) and compare your estimated offset to chrony's reported `System time`.
