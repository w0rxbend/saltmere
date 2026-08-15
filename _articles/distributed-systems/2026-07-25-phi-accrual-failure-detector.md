---
title: "Phi accrual failure detection: suspicion as a number, not a yes/no"
date: 2026-07-25
track: distributed-systems
summary: "A binary heartbeat timeout forces one threshold to serve every network. The phi accrual detector outputs a rising suspicion level computed from past inter-arrival times, so each caller selects its own tolerance for false positives."
reading_time: 6
tags: [failure-detection, heartbeats, cassandra, akka]
sources:
  - title: "Hayashibara et al., The φ Accrual Failure Detector (IEEE SRDS 2004)"
    url: "https://dl.acm.org/doi/10.5555/1032662.1034350"
  - title: "Akka — Phi Accrual Failure Detector (official docs)"
    url: "https://doc.akka.io/libraries/akka-core/current/typed/failure-detector.html"
  - title: "Akka PhiAccrualFailureDetector.scala (source)"
    url: "https://github.com/akka/akka-core/blob/main/akka-remote/src/main/scala/akka/remote/PhiAccrualFailureDetector.scala"
  - title: "Apache Cassandra — Failure detection and recovery (DataStax docs)"
    url: "https://docs.datastax.com/en/cassandra-oss/3.x/cassandra/architecture/archDataDistributeFailDetect.html"
---

**Gist.** A heartbeat-based detector must decide how long silence may last before a peer is declared dead, and a binary timeout hard-codes one answer for every link in the system. The phi accrual detector of Hayashibara et al. (2004) replaces the boolean with a continuous suspicion value, phi, computed from the distribution of previously observed inter-arrival times, so that each caller applies its own threshold. The cost is state and statistics: a per-peer sliding window of samples, a distributional assumption that must hold, and a variance floor without which a steady link produces pathological suspicion.

## The problem a single timeout cannot express

A binary timeout collapses two independent quantities — the observed silence and the plausibility of that silence — into one configured constant. Set it low and a garbage-collection pause or a jittery link convicts a live node; set it high and detection of a genuine crash is correspondingly slow. The same constant must then serve an intra-datacenter link, a cross-region link, and a noisy virtual machine, whose inter-arrival distributions differ by orders of magnitude in both mean and spread. Failure detection over an asynchronous network is unreliable in principle: no finite silence distinguishes a crashed process from a slow one. **The accrual formulation does not remove that impossibility; it exposes the trade-off as a tunable parameter rather than hiding it inside a timeout.**

## From timeout to accrual

Rather than emitting "up" or "down", the detector emits phi, a value that grows with elapsed silence and is scaled by how improbable that silence is given past behaviour:

```
phi(t) = -log10( P_later(t - last_heartbeat) )
```

`P_later(x)` is the probability, under the distribution fitted to previously observed inter-arrival times, that the next heartbeat arrives more than `x` after the previous one. When beats normally arrive every second and one second has elapsed, the silence is unremarkable: `P_later` is near 1 and phi is near 0. When ten seconds have elapsed on a one-second cadence, `P_later` is small and phi rises sharply.

The base-10 logarithm gives phi an operational reading: **phi = k corresponds approximately to a 10^-k probability that a decision to suspect now is mistaken.** phi = 1 is roughly a 10 % chance of a mistaken conviction, phi = 8 roughly 10^-8. A caller therefore does not inherit another component's timeout; it selects the phi threshold matching its own tolerance for false positives, and two callers watching the same peer may legitimately disagree.

The estimate is adaptive in a second respect. **The width of the fitted distribution, not only its mean, enters the calculation.** On a link with wide jitter the learned variance is large, `P_later` decays slowly, and the detector is correspondingly forgiving. On a metronome-steady link the variance is small and phi turns sharp for the same absolute lateness.

## Deployed thresholds

- **Apache Cassandra** feeds gossip heartbeats into the detector; `phi_convict_threshold` in `cassandra.yaml` defaults to **8**. The corresponding tolerated silence is not a fixed number of seconds: it follows from the gossip interval and the observed spread, so the same threshold yields different absolute patience on different links. The DataStax documentation notes that the value is raised in cloud environments to absorb their wider jitter.
- **Akka** uses the detector for cluster DeathWatch. The default threshold is **8** with a heartbeat interval of 1 s, and the documentation recommends **12** on platforms such as AWS EC2 where the network is less predictable.

The same algorithm and the same knob take different values per environment — the degree of freedom a binary timeout cannot represent.

## The variance floor

The load-bearing implementation detail is a lower bound on the standard deviation of the fitted distribution. **As the observed variance approaches zero, the tail probability `P_later` collapses immediately past the mean, and phi diverges after a single millisecond of lateness, convicting every peer on a well-behaved link.** Imposing a minimum standard deviation keeps a healthy margin of tolerance around the mean regardless of how regular the samples are. Akka's `PhiAccrualFailureDetector` carries such a guard; the detector is unusable without one.

A second guard is arithmetic: `P_later` underflows to zero for large elapsed times, and `log10(0)` is undefined, so the probability is clamped to a small positive value before the logarithm is taken. The clamp bounds the maximum reportable phi, which is harmless because every practical threshold lies far below that bound.

### Implementation sketch (Scala)

The sketch keeps a bounded window of recent inter-arrival gaps, fits a normal distribution to them — the assumption made by both the paper and Akka — and reads phi off the normal tail. Sample storage, clock injection and concurrency control are omitted.

```scala
final class PhiAccrual(window: Int = 1000, minStdMs: Double = 50.0):
  private var gaps: Vector[Double] = Vector.empty
  private var last: Option[Long] = None

  def heartbeat(nowMs: Long): Unit =
    last.foreach(prev => gaps = (gaps :+ (nowMs - prev).toDouble).takeRight(window))
    last = Some(nowMs)

  /** Normal(mean, std) CDF via an externally supplied error function `erf`. */
  private def cdf(x: Double, mean: Double, std: Double): Double =
    0.5 * (1.0 + erf((x - mean) / (std * math.sqrt(2.0))))

  def phi(nowMs: Long): Double = last match
    case Some(t) if gaps.sizeIs >= 2 =>
      val elapsed = (nowMs - t).toDouble
      val mean    = gaps.sum / gaps.size
      val varc    = gaps.map(g => (g - mean) * (g - mean)).sum / gaps.size
      // floor prevents a metronome-steady link from producing infinite phi
      val std     = math.max(math.sqrt(varc), minStdMs)
      val later   = 1.0 - cdf(elapsed, mean, std)
      -math.log10(math.max(later, 1e-18))          // clamp avoids log10(0)
    case _ => 0.0
```

`erf` is not in the Scala standard library and must be supplied — for example by an Apache Commons Math `NormalDistribution`, or by a series approximation. Akka's implementation avoids the issue by evaluating a logistic approximation to the normal tail directly rather than calling a CDF.

## Reading a phi trace

Under beats averaging one second with realistic jitter, phi stays near 0 for gaps around the expected interval, passes 1 once the silence becomes mildly surprising, and crosses 8 as the gap stretches well beyond the mean. **Recording the phi trace rather than only the conviction event turns a membership incident into a measurement:** a garbage-collection pause appears as a spike that rises and then decays as beats resume, and its peak states directly how much threshold headroom remained. A cluster whose peaks routinely reach 7 with a threshold of 8 is one pause away from spurious conviction, and no boolean detector surfaces that margin.

The composition used by Cassandra and Akka follows from this: one detector instance per monitored peer, heartbeats fed from the gossip or cluster-heartbeat loop, and a periodic sweep that compares each peer's current phi against the configured threshold.

## Pitfalls

- **A shared threshold across heterogeneous links reintroduces the problem the detector solves.** One global `phi_convict_threshold` tuned for an intra-datacenter link convicts cross-region peers whose inter-arrival spread is far wider.
- **Omitting the variance floor makes a healthy, regular link the worst case.** Variance near zero drives `P_later` to zero immediately past the mean, so phi diverges on the first millisecond of lateness and every peer is convicted.
- **A window that spans a network regime change poisons the estimate.** Samples collected before a routing change or a link degradation keep the fitted mean and variance low, so phi rises faster than the new normal warrants until the old samples age out.
- **Missing heartbeats are not recorded as long intervals.** The window holds gaps between beats that arrived; a peer that was silent and then recovered contributes one very long sample that inflates the variance and blunts subsequent detection, unless the sample is filtered.
- **Suspicion is not a decision.** phi rising past a threshold makes a peer suspect, not dead; a system that acts irreversibly on the first crossing — evicting state, reassigning ownership — cannot recover from a transient pause that the same detector would have reported as decaying.
- **Normality is an assumption, not a measurement.** Inter-arrival times shaped by retransmission or scheduler quantisation are multi-modal, and a normal fit to a multi-modal sample misstates the tail in both directions.
