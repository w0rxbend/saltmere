---
title: "SWIM: gossip-based membership that scales flat"
date: 2026-07-26
track: distributed-systems
summary: "How the SWIM protocol detects failures and spreads membership changes with constant per-node load instead of quadratic heartbeat traffic. You'll learn the ping / ping-req / suspect state machine and build a sketch of it in Python."
reading_time: 5
tags: [swim, membership, failure-detection, gossip, distributed-systems]
sources:
  - title: "SWIM: Scalable Weakly-consistent Infection-style Process Group Membership Protocol (Das, Gupta, Motivala — DSN 2002)"
    url: "https://www.cs.cornell.edu/projects/Quicksilver/public_pdfs/SWIM.pdf"
  - title: "Lifeguard: Local Health Awareness for More Accurate Failure Detection (arXiv 1707.00788)"
    url: "https://arxiv.org/abs/1707.00788"
  - title: "hashicorp/memberlist README"
    url: "https://github.com/hashicorp/memberlist/blob/master/README.md"
  - title: "Making Gossip More Robust with Lifeguard (HashiCorp blog)"
    url: "https://www.hashicorp.com/en/blog/making-gossip-more-robust-with-lifeguard"
  - title: "Consul Gossip Protocol docs"
    url: "https://developer.hashicorp.com/consul/docs/concept/gossip"
---

If a cluster tracks liveness with all-to-all heartbeating, every node pings every other node each interval. That's O(N²) messages: at 1,000 nodes you're moving a million packets per period just to learn nobody died. SWIM — **S**calable **W**eakly-consistent **I**nfection-style **M**embership, from Das, Gupta, and Motivala at DSN 2002 — throws that out. Each node does a *fixed* amount of work per period regardless of cluster size, and failure detection time stays roughly constant too. This complements the phi-accrual failure detector (a smarter *when-is-it-dead* judgement on a single link) and gossip (a smarter *how-do-I-spread-news* mechanism); SWIM combines both ideas into one membership protocol.

## Two components: detection and dissemination

SWIM deliberately separates the two hard problems.

**Failure detection** runs on a fixed protocol period `T`. Each period, a node picks *one* target and probes it — not the whole cluster. That single choice is what makes the load flat: message count per node per period is a constant, versus O(N) per node (O(N²) total) for heartbeating.

| | all-to-all heartbeat | SWIM |
|---|---|---|
| Msgs / node / period | O(N) | O(1) |
| Detection time | ~constant | ~constant (e/(e-1)·T expected) |
| False positives under load | rise sharply | mitigated (suspicion + Lifeguard) |
| Completeness | yes | yes (round-robin target selection) |

**Dissemination** is *infection-style*: membership changes (joins, leaves, deaths) are not multicast. They ride piggybacked on the ping / ack traffic that detection already generates. Like an epidemic, an update spreads to the whole cluster in O(log N) periods, and because it's carried on many independent messages it tolerates packet loss well.

## The probe: direct ping, then indirect ping-req

Here's the clever part that keeps false positives down without extra steady-state cost. When node `A` probes `B` and the direct ping times out, `A` does **not** immediately declare `B` dead. Instead it asks `k` random other members to probe `B` on its behalf — `ping-req(B)`. Those relays ping `B` and forward any ack back. This routes around a congested or lossy `A→B` path and around `A`'s own momentary slowness.

```
period T at node A:
  target = next_round_robin_member()      # round-robin => bounded worst case
  send PING(target); piggyback a few updates
  if ACK within timeout: done
  else:
      relays = k random members (excluding target)
      send PING_REQ(target) to each relay
      if any indirect ACK arrives before T ends: done
      else: mark target SUSPECT   # not dead — yet
```

Round-robin (rather than purely random) target selection guarantees every member is probed within `N` periods, giving a bounded worst-case detection time — an improvement noted in the SWIM paper and adopted by real implementations.

## Suspicion: don't kill on one missed beat

A missed probe might mean death, or might mean a dropped packet or a busy node. SWIM adds a **suspicion** sub-protocol so the cluster reaches consensus-ish agreement before evicting anyone. Three message types flow through the gossip layer, and each member carries an **incarnation number** to arbitrate conflicting claims:

- **Alive(m, inc)** — m is up at incarnation `inc`.
- **Suspect(m, inc)** — someone failed to probe m.
- **Confirm/Dead(m, inc)** — m is declared failed.

The rule: a node can **refute** a suspicion about *itself* by bumping its own incarnation number and gossiping a fresh `Alive` with the higher value. Higher incarnation wins; for equal incarnation, `Suspect` overrides `Alive` and `Confirm` overrides everything.

```python
from enum import Enum

class State(Enum): ALIVE=1; SUSPECT=2; DEAD=3

class Member:
    def __init__(self, id):
        self.id, self.state, self.inc = id, State.ALIVE, 0
        self.suspect_deadline = None

def on_suspect(self, node, msg):        # msg = (id, incarnation)
    m = self.members[msg.id]
    if msg.id == self.id:               # someone suspects ME
        self.inc = max(self.inc, msg.inc) + 1
        self.gossip(("alive", self.id, self.inc))   # refute!
        return
    if msg.inc < m.inc or m.state == State.DEAD:
        return                          # stale, ignore
    if m.state == State.ALIVE:
        m.state, m.inc = State.SUSPECT, msg.inc
        m.suspect_deadline = now() + self.suspicion_timeout
        self.gossip(("suspect", m.id, m.inc))       # spread the suspicion

def on_alive(self, node, msg):
    m = self.members[msg.id]
    if msg.inc > m.inc:                 # newer incarnation overrides suspicion
        m.state, m.inc, m.suspect_deadline = State.ALIVE, msg.inc, None
        self.gossip(("alive", m.id, m.inc))

def tick(self):                         # once per protocol period
    for m in self.members.values():
        if m.state == State.SUSPECT and now() >= m.suspect_deadline:
            m.state = State.DEAD
            self.gossip(("confirm", m.id, m.inc))   # evict
```

If the suspicion timer expires with no refutation, the node transitions `SUSPECT → DEAD` and gossips a `Confirm`, and everyone eventually evicts it.

## In the wild, and the Lifeguard fix

The most widely deployed SWIM implementation is HashiCorp's **memberlist**, embedded by **Serf**, and through it by **Consul** and **Nomad**; Uber's **ringpop** is another well-known one. memberlist keeps the ping/ping-req/suspect core but adds a dedicated periodic gossip channel and TCP-based full-state syncs to speed convergence.

Its most important addition is **Lifeguard** (Dadgar, Phillips, Currey, 2017), which tackles SWIM's real-world weakness: a node that's merely *slow* (CPU starvation, GC pause) fails its own probes and starts wrongly suspecting healthy peers. Lifeguard adds three ideas:

- **Self-Awareness (Local Health Multiplier):** a node that senses it's degraded dials back its own confidence, scaling up its timeouts instead of blaming others.
- **Dogpile:** the suspicion timeout shrinks logarithmically as independent members corroborate a failure — fast on real deaths, patient on maybes.
- **Buddy System:** suspected nodes are notified directly so they can refute quickly, rather than waiting to overhear the gossip.

These cut false positives *and* detection latency simultaneously — a rare win-win.

**Try next:** Take the state machine above, wire it over UDP with `asyncio`, and run 20 local nodes. Then `kill -STOP` one process (simulating a GC pause, not a death) and watch how often the cluster wrongly evicts it — then add Lifeguard's Local Health Multiplier and measure the drop in false positives.
