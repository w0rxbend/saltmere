---
title: "Syncing Physical Clocks: Cristian, Berkeley, and How NTP Works"
date: 2026-07-30
track: distributed-systems
summary: "Wall clocks drift, so a remote machine's time cannot be read — only estimated across a network with unknown latency. This covers Cristian's RTT/2 correction and its error bound, the Berkeley averaging algorithm, and the four-timestamp offset/delay arithmetic NTP runs in production, with the chrony commands that expose real offsets."
reading_time: 7
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

**Gist.** Every quartz oscillator runs at a slightly wrong frequency, so each machine's clock drifts — typically tens of parts per million, which accumulates to seconds per day — and reading a reference clock over a network cannot correct this exactly, because the read itself takes an unknown and possibly asymmetric amount of time. Cristian's algorithm, the Berkeley algorithm and the Network Time Protocol (NTP) all estimate the offset from a round-trip measurement and correct for it. The cost is an irreducible uncertainty bounded by the round-trip time (RTT), plus a systematic error equal to half the path asymmetry that no amount of sampling removes.

This is a different problem from logical clocks. Lamport and vector clocks establish causal *order* without reference to real time; physical synchronization aims at the wall-clock value itself, as close to Coordinated Universal Time (UTC) as the network permits.

## Cristian's algorithm: correct for the round trip

The mechanism, from Flaviu Cristian's 1989 *Probabilistic clock synchronization*, measures the round trip and attributes half of it to the reply leg. The client stamps `T0` before sending the request, the server replies with its own time `T_server`, and the client stamps `T1` on receipt:

```
RTT   = T1 - T0
t_est = T_server + RTT / 2
```

The local clock is set to `t_est`. The `RTT/2` term follows from **assuming the two legs are symmetric**: under that assumption the server's timestamp was generated approximately `RTT/2` before `T1`.

The load-bearing property is the **explicit error bound**. Let `min` be the smallest possible one-way transit time. The server's reading must have occurred somewhere in the interval `[T0 + min, T1 - min]`, so the true time lies within

```
±(RTT/2 - min)
```

of the estimate. The bound depends only on the observed RTT and the physical minimum, not on any assumption about the distribution of delay. **A smaller RTT yields a tighter bound**, which is why Cristian's probabilistic refinement issues many requests and retains only the response with the **lowest RTT** — the sample least polluted by queueing. That minimum-delay selection rule reappears in NTP's clock filter.

The assumption of symmetry is the limitation the model bakes in. Asymmetric routing — a fast path outbound, a congested path inbound — skews the estimate by half the asymmetry, and repeated sampling does not detect it, because every sample carries the same bias. On the public internet this is the dominant error source.

### Implementation sketch (Scala)

The sketch keeps the minimum-RTT sample and returns the offset together with the half-RTT bound. `queryServer` stands for whatever transport delivers the remote clock reading in milliseconds since the epoch.

```scala
final case class Sample(rttMillis: Long, offsetMillis: Long):
  /** Uncertainty on the offset, before subtracting the physical minimum transit. */
  def boundMillis: Long = rttMillis / 2

def cristianSync(queryServer: () => Long, probes: Int = 8): Sample =
  def probe(): Sample =
    val t0 = System.currentTimeMillis()
    val tServer = queryServer()
    val t1 = System.currentTimeMillis()
    val rtt = t1 - t0
    // The server stamped tServer roughly rtt/2 before t1, under leg symmetry.
    Sample(rtt, tServer + rtt / 2 - t1)

  (1 to probes).map(_ => probe()).minBy(_.rttMillis)
```

Two details are deliberate. The samples are compared by RTT rather than averaged: averaging mixes queue-inflated samples into the estimate, whereas the minimum is the sample whose bound is tightest. And `System.currentTimeMillis` is the correct source here despite being non-monotonic, because the quantity under measurement *is* the wall clock; a monotonic source such as `System.nanoTime` would be the right choice for the RTT interval alone.

## The Berkeley algorithm: internal agreement without UTC

Cristian's model requires an authoritative time server. The Berkeley algorithm of Gusella and Zatti — the `TEMPO` daemon in 4.3BSD — addresses the opposite case: a cluster with no reliable external reference that needs its members to agree **with each other**. It is master-driven and averaging:

1. A master polls every node for its clock, applying Cristian-style round-trip correction to each reading to account for message delay.
2. The master **averages** the readings after discarding outliers that lie too far from the rest, so a single wildly wrong clock cannot drag the group.
3. It computes each node's offset from that average and returns a **relative adjustment** — "slow down by 40 ms" — never an absolute time.

Returning adjustments rather than absolute times has a direct consequence: **nodes never jump their clocks backward**, they slew toward the agreed value. The master is itself an ordinary node; if it fails, an election selects another. The result is tight *internal* synchronization that holds even when the whole cluster is disconnected from the outside world, at the cost of no relationship to UTC.

## NTP: Cristian's estimator hardened for the internet

NTP version 4, specified in RFC 5905 (June 2010), uses four timestamps instead of two so that the server's own processing time cancels. With `T1` the client send time, `T2` the server receive time, `T3` the server send time and `T4` the client receive time:

```
offset θ = ½·[(T2 - T1) + (T3 - T4)]
delay  δ = (T4 - T1) - (T3 - T2)
```

`δ` subtracts the server's think-time `(T3 - T2)` from the total elapsed interval, leaving the network round trip alone; `θ` is the mean of the two one-way offset estimates. NTP collects many `(θ, δ)` pairs and, following Cristian, its clock filter prefers the samples with **lowest delay**.

Servers form a **stratum** hierarchy: stratum 0 is a reference such as a Global Positioning System (GPS) receiver or an atomic clock, stratum 1 synchronizes directly to it, and each further level adds one. A client cross-checks several servers and discards "falsetickers" — sources whose claimed intervals are inconsistent with the majority — before disciplining its clock. Ordinary discipline proceeds by **slewing the operating-system tick rate rather than stepping the clock**, so monotonicity is preserved for applications; implementations step only when the offset is too large to slew away in reasonable time.

Accuracy in practice: tens of milliseconds down to single-digit milliseconds over the public internet, sub-millisecond on a local network, and tens of microseconds with a local reference and a good implementation. chrony's own published comparison of NTP implementations reports a **smaller residual offset than classic `ntpd`** on a permanent link with network jitter, and chrony remains usable on intermittent connections where `ntpd` degrades, because chrony models the clock's rate and can correct from that model between measurements.

The offset a running system applies is observable:

```console
$ chronyc tracking
Reference ID    : A29FC87B (time.cloudflare.com)
Stratum         : 3
System time     : 0.000023019 seconds slow of NTP time
RMS offset      : 0.000041991 seconds
Root delay      : 0.02216 seconds
Frequency       : 12.482 ppm slow
```

`System time` is the current offset — about 23 µs in this sample. `Frequency` is the measured drift chrony compensates for, direct evidence that the local oscillator never ran at exactly the nominal rate: at 12.482 ppm the uncorrected clock would drift `ppm × 86400 / 1e6 ≈ 1.08` seconds per day. `chronyc sources -v` reports per-server delay and offset, the raw material of the formulas above.

## Pitfalls

- **Path asymmetry produces a constant offset error of half the asymmetry.** Minimum-RTT filtering removes queueing noise but not bias, because a systematically longer return path inflates every sample identically.
- **Treating `RTT/2` as the accuracy is optimistic.** The bound is `±(RTT/2 - min)`, and it applies only under the symmetry assumption; a large RTT means a large bound even when successive estimates agree closely.
- **Stepping the clock backward breaks interval measurements.** Code that subtracts two wall-clock readings can observe a negative duration; Berkeley's relative adjustments and NTP's slewing both keep the correction monotone and so avoid this.
- **A stratum-1 server is not automatically accurate.** Stratum counts hops to a reference, not error; a stratum-1 server behind a congested asymmetric path can be worse than a nearby stratum-3 server.
- **A single time source cannot be cross-checked.** Falseticker detection requires multiple servers; with one configured source, a server whose clock is wrong is followed rather than rejected.
- **Averaging round-trip samples instead of taking the minimum admits queueing delay into the estimate.** A congested probe inflates both the RTT and the error bound, and averaging propagates that inflation to the final offset.
