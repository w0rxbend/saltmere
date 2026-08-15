---
title: "SWIM: gossip-based membership that scales flat"
date: 2026-07-26
track: distributed-systems
summary: "How the SWIM protocol detects failures and spreads membership changes with constant per-node load instead of quadratic heartbeat traffic: the ping / ping-req / suspect state machine, incarnation numbers, and the Lifeguard extensions."
reading_time: 6
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

**Gist.** All-to-all heartbeating costs O(N²) messages per period, so a 1,000-node cluster moves on the order of a million packets per period to establish that nothing has changed. SWIM — **S**calable **W**eakly-consistent **I**nfection-style **M**embership, from Das, Gupta and Motivala (DSN 2002) — replaces that with one randomly selected probe per node per period plus epidemic dissemination of membership updates, giving constant per-node load and expected detection time independent of cluster size. The cost is weak consistency: membership views converge only eventually, and a merely slow node can be suspected and evicted while still running.

SWIM sits between two neighbouring ideas. A phi-accrual detector improves the *when is a single link dead* judgement; gossip improves the *how does news spread* mechanism. SWIM composes both into one membership protocol.

## Two components: detection and dissemination

The protocol separates failure detection from the propagation of membership changes.

**Failure detection** runs on a fixed protocol period `T`. In each period a node selects **one** target and probes it, rather than the whole cluster. Message count per node per period is therefore constant, against O(N) per node — O(N²) in total — for all-to-all heartbeating.

| | all-to-all heartbeat | SWIM |
|---|---|---|
| Msgs / node / period | O(N) | O(1) |
| Detection time | ~constant | ~constant (e/(e−1)·T expected) |
| False positives under load | rise sharply | mitigated (suspicion + Lifeguard) |
| Completeness | yes | yes (round-robin target selection) |

The expected detection time **e/(e−1)·T** follows from each node being chosen as a probe target with probability 1/(N−1) per period by each of the other N−1 members: the probability of remaining unprobed after one period tends to 1/e as N grows.

**Dissemination** is *infection-style*. Joins, leaves and deaths are not multicast; they are piggybacked on the ping and ack packets that detection already sends. As in an epidemic, an update reaches the whole cluster in **O(log N) periods**, and because the update rides on many independent messages, loss of any single packet does not stall propagation.

## The probe: direct ping, then indirect ping-req

When node `A` probes `B` and the direct ping times out, `A` does **not** declare `B` dead. It asks `k` randomly chosen other members to probe `B` on its behalf with `ping-req(B)`. Each relay pings `B` and forwards any ack back to `A`. This distinguishes **a broken A→B path, or momentary slowness at A, from a dead B**, at a cost that is paid only on the failure path and not in steady state.

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

Round-robin target selection, rather than independent uniform sampling, guarantees **every member is probed within N periods**, which bounds the worst-case detection time instead of leaving it to the tail of a geometric distribution. The SWIM paper describes this refinement, and deployed implementations adopt it.

## Suspicion and incarnation numbers

A missed probe may indicate death, a dropped packet, or a busy process. The **suspicion** sub-protocol delays eviction so the rest of the cluster has an opportunity to contradict the claim. Three assertions circulate through the gossip layer, each carrying an **incarnation number** owned by the member it describes:

- **Alive(m, inc)** — m is up at incarnation `inc`.
- **Suspect(m, inc)** — a probe of m failed.
- **Confirm/Dead(m, inc)** — m is declared failed.

The arbitration rule is a total order over these assertions. **Only m itself may increment m's incarnation number.** A node that hears a `Suspect` about itself refutes it by raising its own incarnation above the suspected one and gossiping a fresh `Alive`. Higher incarnation wins; at equal incarnation, `Suspect` overrides `Alive`, and `Confirm` overrides everything. The invariant is that **`Confirm` is terminal for the incarnation it names**: a `Confirm(m, inc)` overrides `Alive(m, i)` and `Suspect(m, i)` for every `i ≤ inc`, so a member that has been declared dead cannot be revived by replaying older assertions. Returning to the cluster requires an incarnation strictly above the confirmed one.

If the suspicion timer expires with no refutation, the member transitions `SUSPECT → DEAD` and a `Confirm` is gossiped; every node that receives it evicts the member.

### Implementation sketch (Scala)

The state machine below is the merge function that every incoming assertion passes through. Transport, timers and piggyback encoding are omitted.

```scala
enum State: case Alive, Suspect, Dead

final case class Member(id: String, state: State, inc: Long, deadline: Option[Long])

enum Msg:
  case Alive(id: String, inc: Long)
  case Suspect(id: String, inc: Long)
  case Confirm(id: String, inc: Long)

class Node(val self: String, suspicionTimeout: Long):
  private var members: Map[String, Member] = Map.empty
  private var myInc: Long = 0

  def merge(msg: Msg, now: Long): Unit = msg match
    case Msg.Suspect(id, inc) if id == self =>
      myInc = math.max(myInc, inc) + 1        // only the subject may raise its incarnation
      gossip(Msg.Alive(self, myInc))
    case Msg.Suspect(id, inc) =>
      members.get(id).filter(m => m.state == State.Alive && inc >= m.inc).foreach { m =>
        members += id -> m.copy(state = State.Suspect, inc = inc, deadline = Some(now + suspicionTimeout))
        gossip(msg)
      }
    case Msg.Alive(id, inc) =>
      members.get(id).filter(m => m.state != State.Dead && inc > m.inc).foreach { m =>
        members += id -> m.copy(state = State.Alive, inc = inc, deadline = None)  // refutation clears the timer
        gossip(msg)
      }
    case Msg.Confirm(id, inc) =>
      members.get(id).filter(_.inc <= inc).foreach { m =>
        members += id -> m.copy(state = State.Dead, inc = inc, deadline = None)
        gossip(msg)                            // terminal for every incarnation <= inc
      }

  def tick(now: Long): Unit =
    members.values
      .filter(m => m.state == State.Suspect && m.deadline.exists(now >= _))
      .foreach(m => merge(Msg.Confirm(m.id, m.inc), now))

  private def gossip(msg: Msg): Unit = ???     // piggybacked on the next ping/ack
```

## Deployed implementations and Lifeguard

The most widely deployed open-source SWIM implementation is HashiCorp's **memberlist**, embedded by **Serf** and through it by **Consul** and **Nomad**; Uber's **ringpop** is another. memberlist retains the ping / ping-req / suspect core and adds a dedicated periodic gossip channel and TCP-based full-state synchronisation.

Its principal extension is **Lifeguard** (Dadgar, Phillips, Currey, 2017), which addresses the case where **a node that is merely slow — CPU starvation, a long garbage-collection pause — fails its own probes and begins suspecting healthy peers**. Lifeguard adds three mechanisms:

- **Self-Awareness (Local Health Multiplier):** a node that observes its own probes failing scales up its timeouts, reducing the weight of its own judgements rather than attributing the failures to peers.
- **Dogpile:** the suspicion timeout shrinks logarithmically as independent members corroborate the suspicion, so a genuinely dead node is confirmed sooner than an ambiguous one.
- **Buddy System:** a suspected node is notified directly instead of waiting to overhear the suspicion through gossip, shortening the path to refutation.

The Lifeguard paper reports a large reduction in false positives without a corresponding increase in detection time. The three mechanisms are described as separately adoptable, and memberlist ships with them.

## Pitfalls

- **Treating membership as consistent.** Two nodes can hold different views of the same member for O(log N) periods; a leader election or shard assignment computed directly from the local view will disagree across the cluster during that window.
- **A suspicion timeout shorter than a garbage-collection pause.** A stop-the-world pause longer than the timeout produces `SUSPECT → DEAD` for a live process, which then rejoins and forces the cluster to re-converge — a flap loop under sustained memory pressure.
- **Restarting a process at its old incarnation.** A restarted node that rejoins under the same identity and an incarnation no higher than the confirmed one is discarded by every peer that still holds the `Confirm`.
- **Setting `k` (the ping-req fan-out) to zero or one.** With no indirect probes, every dropped ping becomes a suspicion; with one relay, a single unlucky relay choice reproduces the same false positive.
- **Assuming round-robin selection is optional.** Under purely random target selection, some member is left unprobed for an unbounded number of periods with non-zero probability, so worst-case detection time is no longer bounded by N periods.
- **Attributing local probe failures to the target.** Without Lifeguard's Local Health Multiplier, the single most degraded node in the cluster generates the largest number of suspicions, because it fails the most probes.
