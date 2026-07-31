---
title: "Ricart–Agrawala: mutual exclusion with no coordinator and no token"
date: 2026-07-31
track: distributed-systems
summary: "Ricart & Agrawala (1981) let N processes share a critical section using only REQUEST and REPLY messages, ordered by Lamport timestamps and enforced by deferring replies. Here is why it needs a logical clock, how deferral guarantees safety, and why 2(N-1) messages is optimal for a permission-based scheme."
reading_time: 5
tags: [mutual-exclusion, ricart-agrawala, coordination, lamport-clocks, distributed-systems]
sources:
  - title: "An Optimal Algorithm for Mutual Exclusion in Computer Networks (CACM 24(1):9–17, 1981) — Glenn Ricart & Ashok K. Agrawala"
    url: "https://dl.acm.org/doi/10.1145/358527.358537"
  - title: "Ricart–Agrawala algorithm — Wikipedia (message types, deferral, 2(N-1) cost)"
    url: "https://en.wikipedia.org/wiki/Ricart%E2%80%93Agrawala_algorithm"
  - title: "Distributed Systems (4th ed.) — van Steen & Tanenbaum, Ch. 6 Coordination"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Lamport's logical clocks and Ricart–Agrawala in Python — bytepawn (engineering write-up)"
    url: "https://bytepawn.com/lamport-logical-clocks-distributed-mutual-exclusion.html"
  - title: "Ricart–Agrawala Algorithm in Mutual Exclusion in Distributed System — GeeksforGeeks"
    url: "https://www.geeksforgeeks.org/operating-systems/ricart-agrawala-algorithm-in-mutual-exclusion-in-distributed-system/"
---

A single lock is easy when everyone shares memory. Spread the processes across a network and "who holds the lock right now?" becomes a genuine distributed-agreement problem. van Steen & Tanenbaum's coordination chapter lays out three shapes of answer, and **Ricart–Agrawala (1981)** is the one that manages to be fully decentralized without a token that can get lost.

## Three ways to guard a critical section

- **Centralized / coordinator.** One process is the lock server: ask, wait for a grant, send a release when done. Three messages per entry, dead simple, but the coordinator is a single point of failure and a bottleneck, and its crash is ambiguous ("no reply" could mean *denied* or *dead*).
- **Token ring.** A single token circulates a logical ring; hold it, you may enter. No starvation, but the token can be lost (now you must regenerate it *and* be sure the old one is gone), and you may wait a full lap for a token even when nobody else wants it.
- **Permission-based (Ricart–Agrawala).** No coordinator, no token. To enter, you ask *everyone* and enter only once *everyone* agrees. This is where the interesting mechanics live.

## Two messages, one clock

Ricart–Agrawala uses exactly two message types. A process wanting the critical section broadcasts a **REQUEST** carrying `(timestamp, pid)` to the other N−1 processes. Each recipient eventually sends back a **REPLY**. When you have collected REPLYs from all N−1 peers, you enter. That is the whole protocol — the subtlety is entirely in *when* a recipient replies.

The `timestamp` is a **Lamport logical clock**, and it is not optional. Two processes can request "at the same time" with no global clock to break the tie; the algorithm needs a *total order* on requests so that exactly one wins every conflict, consistently, at every node. Lamport timestamps provide a happens-before order, and `(timestamp, pid)` with the pid as tiebreaker makes it total. Strip the clock out and two requests become incomparable — both sides could defer to each other (deadlock) or both proceed (a safety violation).

## Deferred replies are the lock

When a process receives a REQUEST, it decides between replying now or *deferring*:

- If it is **not** interested in the critical section → REPLY immediately.
- If it **is** in the critical section → defer (hold the REPLY until it exits).
- If it is **also requesting** → compare timestamps. If the incoming request is *earlier* (lower `(ts, pid)`), it has priority, so REPLY now. Otherwise the recipient's own request wins, so **defer**.

Mutual exclusion falls out of this: whoever owns the lowest outstanding timestamp receives REPLYs from everyone (nobody with a reason to defer outranks them) and enters. Every competitor with a higher timestamp is missing at least that process's REPLY and blocks. When the holder exits, it flushes every deferred REPLY, releasing the next-lowest requester. No two processes can both collect a full set of REPLYs at once.

```python
def request_cs(self):
    self.clock += 1
    self.req_ts = (self.clock, self.pid)   # my request's Lamport stamp
    self.requesting = True
    self.replies = 0
    for p in self.peers:                    # broadcast to the other N-1
        p.send("REQUEST", ts=self.req_ts)

def on_request(self, ts):                   # ts = (clock, pid) of sender
    self.clock = max(self.clock, ts[0]) + 1 # advance logical clock
    # Defer if I'm in the CS, or I want it and outrank the sender.
    defer = self.in_cs or (self.requesting and self.req_ts < ts)
    if defer:
        self.deferred.append(ts[1])         # remember to reply later
    else:
        self.reply(ts[1], "REPLY")

def on_reply(self):
    self.replies += 1
    if self.replies == len(self.peers):     # heard yes from everyone
        self.enter_cs()

def release_cs(self):
    self.in_cs = False
    self.requesting = False
    for pid in self.deferred:               # flush every held REPLY
        self.reply(pid, "REPLY")
    self.deferred.clear()
```

## Why 2(N−1) is the point

Each entry costs one REQUEST and one REPLY to each of the N−1 peers: **2(N−1) messages** per critical-section access, with a synchronization delay of a single message round trip. That was the "optimal" in the paper's title — for a scheme where every process participates in every decision, you cannot do fewer than one message each way per peer. Compared to the coordinator's flat 3 messages it scales badly (every entry now touches the whole group), but it buys you a genuinely symmetric system with no single point of failure and an explicit total order that also gives you FIFO-by-timestamp fairness and freedom from starvation.

The catch is fault tolerance: a *single* crashed process never sends its REPLY, so it stalls the entire group. The classic fix — Maekawa's √N quorums, or layering failure detection on top — is exactly why real systems tend to reach for a lease in a consensus service instead. But as a demonstration that mutual exclusion needs *ordering*, not a *master*, Ricart–Agrawala is hard to beat.

**Try next:** Implement the handlers above for four in-process nodes with randomized message delay and a shared counter as the critical section. First run it *without* deferral (reply immediately, always) and watch the counter lose increments under contention; then switch deferral on and confirm it reaches N×increments exactly. Finally, make one node stop sending REPLYs and observe the whole group wedge — the single-crash weakness that quorum variants exist to remove.
