---
title: "Bully and Ring Election: the classic coordinator algorithms"
date: 2026-07-30
track: distributed-systems
summary: "Before Raft there were the Bully algorithm (Garcia-Molina, 1982) and the Ring algorithm — two ways for a group of numbered processes to agree on the highest-id survivor as coordinator. Here are both, with runnable handlers, a crash walk-through, and the message counts that decide which one you want."
reading_time: 5
tags: [leader-election, bully-algorithm, ring-algorithm, coordination, garcia-molina, message-complexity]
sources:
  - title: "Elections in a Distributed Computing System (IEEE Transactions on Computers, C-31, 1982) — Hector Garcia-Molina"
    url: "https://dl.acm.org/doi/10.1109/TC.1982.1675885"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Bully algorithm — Wikipedia (assumptions, messages, O(n^2) worst case)"
    url: "https://en.wikipedia.org/wiki/Bully_algorithm"
  - title: "Chang and Roberts algorithm — Wikipedia (ring election, 3N-1 worst case)"
    url: "https://en.wikipedia.org/wiki/Chang_and_Roberts_algorithm"
  - title: "CS677 Lecture 14: Leader Election (lecture notes) — Prashant Shenoy, UMass"
    url: "https://lass.cs.umass.edu/~shenoy/courses/spring22/lectures/Lec14_notes.pdf"
---

Plenty of distributed algorithms need exactly one process to play a special role — the lock server, the sequencer, the replication primary. When that process dies, the survivors must pick a replacement *without* a human in the loop and *without* two of them both deciding they're in charge. This is **leader (coordinator) election**. Modern systems reach for Raft or a lease in etcd, but the two textbook algorithms — **Bully** (Garcia-Molina, 1982) and **Ring** — are still worth knowing cold: they are small, they expose the assumptions consensus quietly relies on, and their message counts teach you why the naive approach costs O(n²).

## The setup

Assume `n` processes, each with a **unique, comparable id**, and every process knows the ids and addresses of all the others. The election rule is deliberately trivial: **the highest-id process that is currently alive becomes coordinator.** That leaves only the hard part — detecting the old coordinator's death and converging on the new one — so both algorithms need a **synchronous** model: bounded message delay and a timeout-based failure detector, so "no reply within T" can be treated as "dead." Drop synchrony and neither algorithm is safe; that gap is exactly what Raft's terms and randomized timeouts exist to fill.

## The Bully algorithm

Three message types: **ELECTION** (I'm starting an election, sent to higher ids), **OK / ANSWER** (I'm alive and outrank you, stand down), and **COORDINATOR** (I won). Any process that notices the coordinator is unreachable starts an election:

```python
def start_election(self):
    higher = [p for p in self.peers if p.id > self.id]
    if not higher:                       # I'm the top id
        return self.declare_victory()
    self.awaiting_ok = True
    for p in higher:
        p.send("ELECTION", src=self.id)
    # wait T for any OK
    schedule(T, self._election_timeout)

def on_election(self, src):              # from a LOWER id
    self.reply(src, "OK")                # "I outrank you, back off"
    if not self.in_election:
        self.start_election()            # bully upward myself

def _election_timeout(self):
    if self.awaiting_ok:                 # nobody higher answered
        self.declare_victory()

def declare_victory(self):
    self.coordinator = self.id
    for p in self.peers:
        if p.id != self.id:
            p.send("COORDINATOR", src=self.id)

def on_coordinator(self, src):
    self.coordinator = src               # accept the winner
    self.in_election = False
```

A process that gets an OK knows someone bigger is alive, so it gives up and simply waits for a COORDINATOR message. If that never arrives (the higher process it heard from also died), a second timeout makes it restart the whole election. The name is apt: the biggest live process always "bullies" its way to the top by shouting everyone else down.

## Walk-through: the top node crashes

Cluster of five, ids `{1,2,3,4,5}`, and `5` is the coordinator. `5` crashes. Suppose `2` notices first (a request to `5` times out):

1. `2` sends ELECTION to `3, 4, 5`.
2. `3` and `4` reply **OK**; `5` is silent. `2` stands down and waits.
3. `3` and `4`, having answered, each start their own election. `3` sends ELECTION to `4, 5`; `4` replies OK, so `3` stands down. `4` sends ELECTION to `5` only.
4. `4` hears nothing back before `T`. Its timeout fires → `4` **declares victory** and sends COORDINATOR to `1, 2, 3, 5`.
5. Everyone records `4` as coordinator. When `5` reboots it starts its own election, wins (highest id), and takes over again.

Notice how much chatter that was for four survivors — and that the lowest-id initiator triggers the most. That's the worst case: **if the lowest id starts, it provokes O(n²) messages** (every process ends up messaging every higher process). Best case, when the process just below the dead coordinator initiates, it's O(n): a handful of ELECTIONs, one round of OKs, one COORDINATOR broadcast.

## The Ring algorithm

Arrange the processes in a **logical ring** (each knows its successor; skip dead ones to the next live successor). Election messages travel one direction only, carrying an accumulating list of ids:

```python
def start_election(self):
    self.forward("ELECTION", ids=[self.id])

def on_election(self, ids):
    if self.id not in ids:
        ids.append(self.id)              # add myself, keep circulating
        self.forward("ELECTION", ids=ids)
    else:                                # message came all the way back
        leader = max(ids)
        self.forward("COORDINATOR", ids=ids, leader=leader)

def on_coordinator(self, leader):
    self.coordinator = leader
    if leader != self.id:                # pass the announcement on
        self.forward("COORDINATOR", leader=leader)
```

The ELECTION message goes once around, gathering every live id. When it returns to the initiator, that node picks `max(ids)` and sends a **COORDINATOR** message around the ring to announce the winner and let everyone stop. Two laps of the ring: **2(n−1) ≈ O(2n) messages**, deterministic regardless of who starts. (The classic Chang–Roberts variant instead forwards only ids larger than its own and swallows smaller ones, so the message shrinks as it travels — worst case 3N−1 sequential messages, but the same O(n) order.)

## Which one, and why

| | Bully | Ring |
|---|---|---|
| Messages (worst case) | **O(n²)** (lowest id starts) | **2(n−1) ≈ O(2n)** |
| Messages (best case) | O(n) | 2(n−1) |
| Topology known | full membership | successor only |
| Convergence | fast when a high id is alive | one guaranteed round-trip |
| Failure model | synchronous + timeouts | synchronous + timeouts |

Bully wins on latency — a high-id survivor declares victory almost immediately — at the cost of a message blow-up when a low id initiates. Ring trades that for predictable, linear traffic, but must rebuild the ring when nodes die and can be slow because it always pays a full two laps. Both share the fatal limitation that made Raft necessary: they assume reliable delivery and bounded timeouts, so a network partition can hand you **two coordinators**, one per side, each certain it saw everyone above it die. Neither has a term number or a quorum to fence off the loser.

**Try next:** Implement the Bully handlers above for five in-process nodes with simulated message delay, kill the highest id, and count the actual ELECTION/OK/COORDINATOR messages when the *lowest* id initiates versus the *second-highest* — then watch the count collapse from O(n²) toward O(n). For a sharper lesson, drop one OK message on the floor and observe a lower node wrongly crown itself: the exact split-brain that a quorum-based protocol would have prevented.
