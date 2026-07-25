---
title: "Phi accrual failure detection: suspicion as a number, not a yes/no"
date: 2026-07-25
track: distributed-systems
summary: "A binary heartbeat timeout forces one threshold to serve every network. The phi accrual detector outputs a rising suspicion level computed from past inter-arrival times, so callers pick their own risk. Here's the math and a compact Python detector."
reading_time: 5
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

Every heartbeat-based detector faces the same question: how long do I wait for a beat before declaring the node dead? A binary timeout hard-codes one answer. Set it low and a GC pause or a jittery link convicts a healthy node; set it high and you're slow to react to a real crash. Worse, that single number has to be right for the datacenter *and* the cross-region link *and* the noisy VM. Chapter on fault tolerance in van Steen & Tanenbaum frames failure detection as fundamentally unreliable; Hayashibara et al. (2004) made it *tunable per caller* by replacing the boolean with a continuous suspicion level, phi.

## From timeout to accrual

Instead of "up" or "down", the detector outputs phi: a number that rises the longer it's been since the last heartbeat, scaled by how *surprising* that silence is given past behavior. Formally,

```
phi(t) = -log10( P_later(t - last_heartbeat) )
```

where `P_later(x)` is the probability, under the distribution of previously observed inter-arrival times, that the *next* heartbeat is still more than `x` later than the previous one. If beats normally arrive every 1s and it's been 1s, silence is unremarkable — `P_later` is near 1 and phi is near 0. If it's been 10s on a 1s cadence, `P_later` is tiny and phi shoots up.

The log base 10 gives phi a clean operational meaning: **phi = k roughly means a `10^-k` chance you're wrong to suspect right now.** phi=1 is a ~10% chance of a mistaken conviction, phi=8 is ~10^-8. So a caller doesn't inherit someone's timeout — it picks the phi threshold matching its own tolerance for false positives.

## Who runs it, and at what threshold

- **Cassandra** feeds gossip heartbeats into this detector; `phi_convict_threshold` in `cassandra.yaml` defaults to **8**, which lets a node go quiet for roughly 18 seconds before it's convicted. Cloud/cross-DC deploys bump it to 10–12 to survive jitter.
- **Akka** uses it for cluster DeathWatch. The default threshold is **8**, heartbeat interval 1s, and the docs explicitly recommend **12** on platforms like AWS EC2 where the network is less predictable.

Same algorithm, same knob, different value per environment — which is exactly the point a binary timeout can't express.

## A compact detector in Python

Keep a sliding window of recent intervals, assume they're roughly normal (what the paper and Akka both do), and compute phi from the normal tail:

```python
import math
from collections import deque

def _cdf(x, mean, std):                       # Normal(mean, std) CDF
    return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))

class PhiAccrualDetector:
    def __init__(self, window=1000, min_std_ms=50.0):
        self.intervals = deque(maxlen=window) # recent inter-arrival gaps
        self.min_std_ms = min_std_ms          # floor so a steady link
        self.last = None                      #   still tolerates jitter

    def heartbeat(self, now_ms):
        if self.last is not None:
            self.intervals.append(now_ms - self.last)
        self.last = now_ms

    def phi(self, now_ms):
        if self.last is None or len(self.intervals) < 2:
            return 0.0
        elapsed = now_ms - self.last
        mean = sum(self.intervals) / len(self.intervals)
        var = sum((x - mean) ** 2 for x in self.intervals) / len(self.intervals)
        std = max(math.sqrt(var), self.min_std_ms)
        p_later = 1.0 - _cdf(elapsed, mean, std)      # P(next beat even later)
        return -math.log10(max(p_later, 1e-18))       # clamp avoids log(0)
```

The `min_std_ms` floor matters: on a metronome-steady link the variance collapses toward zero, and without a floor phi would explode on the first millisecond of lateness and convict everything. Akka's implementation carries the same guard.

## Watch it move

```python
import random
d, t = PhiAccrualDetector(), 0.0
for _ in range(200):                      # beats ~1s apart, real jitter
    t += random.gauss(1000, 150)
    d.heartbeat(t)
for gap in (900, 1200, 1600, 2200, 3000): # ms of silence since last beat
    print(gap, round(d.phi(t + gap), 2))
```

phi stays near 0 around the expected 1s gap, passes 1 as the silence gets mildly surprising, and crosses 8 as it stretches past ~2s — cross the threshold you chose and you act. The jitter matters: on a noisy link the learned variance is wide, so `P_later` collapses more slowly and the detector is automatically more forgiving. On a metronome-steady link the variance is tiny and phi turns sharp — which is why the `min_std_ms` floor exists.

**Try next:** wire this into a gossip loop — one detector per peer, a background sweep that convicts any peer whose phi exceeds a configurable threshold, and log the phi trace so you can *see* a GC pause spike suspicion without tripping it. That's the shape of Cassandra's and Akka's membership.
