---
title: "Vector clocks in ~40 lines — and what they buy you over Lamport clocks"
date: 2026-07-24
track: distributed-systems
summary: "Lamport clocks tell you that events might be causally related; vector clocks tell you whether they actually are. Here's the smallest implementation that makes the difference click."
reading_time: 5
tags: [causality, logical-clocks, coordination, van-steen]
sources:
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.), §5.2 Logical clocks"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "Lamport, Time, Clocks, and the Ordering of Events in a Distributed System (1978)"
    url: "https://lamport.azurewebsites.net/pubs/time-clocks.pdf"
---

There is no global clock in a distributed system, so "when did this happen?" is the wrong question. The useful question is "did A *happen-before* B?" — could A have influenced B? Chapter 5 of van Steen & Tanenbaum builds two answers to that: Lamport clocks and vector clocks. The difference between them is the whole point, and it fits in a page of code.

## Lamport clocks: necessary but not sufficient

A Lamport clock is one integer per process. Increment on every local event; attach it to every message; on receipt, set `clock = max(local, received) + 1`. This guarantees: if A → B (A happened-before B) then `L(A) < L(B)`.

The catch is the converse is **false**. `L(A) < L(B)` does *not* mean A → B — they might be concurrent and just happened to get different numbers. So Lamport clocks can order events into *a* consistent total order, but they cannot tell you whether two events were genuinely causally related or merely concurrent. For conflict detection (think: two replicas editing the same key), that distinction is exactly what you need.

## Vector clocks: capturing causality exactly

Give each of `N` processes a vector of `N` counters. Process `i` bumps its own slot on a local event, ships the whole vector on send, and on receive takes the element-wise `max` then bumps its own slot. Now the ordering is *exact*:

- `A → B`  iff  `V(A) < V(B)` (every slot ≤, at least one strictly less)
- otherwise A and B are **concurrent** (neither dominates)

```python
class VectorClock:
    def __init__(self, n, i):
        self.i = i                 # this process's index
        self.v = [0] * n           # one counter per process

    def local(self):              # a local event happened
        self.v[self.i] += 1
        return list(self.v)

    def send(self):               # stamp an outgoing message
        self.v[self.i] += 1
        return list(self.v)

    def recv(self, other):        # merge a received stamp
        self.v = [max(a, b) for a, b in zip(self.v, other)]
        self.v[self.i] += 1
        return list(self.v)

def happens_before(a, b):
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))

def concurrent(a, b):
    return not happens_before(a, b) and not happens_before(b, a)
```

That is the entire mechanism. Everything fancy — Dynamo-style sibling detection, CRDT causal delivery, session guarantees — is this idea with bookkeeping bolted on.

## A five-minute experiment

Simulate three processes by hand. Have P0 do a local event, send to P1; P1 receives and sends to P2; meanwhile P2 does an independent local event *before* that message arrives. Print the stamps and run `concurrent()` on P2's independent event versus P0's first event. You will see the vectors correctly flag them as concurrent — something a single Lamport integer physically cannot represent.

## Where it bites in real systems

The cost is `O(N)` space per stamp, and `N` is the number of *writers*, not requests — so vector clocks are cheap for a handful of replicas and painful for millions of clients. That single trade-off explains a lot of production design: Dynamo caps versions and does sibling reconciliation; many systems fall back to "dotted version vectors" or hybrid logical clocks (HLCs) that glue a physical timestamp onto a Lamport counter to bound the size. Knowing why they retreat from full vector clocks is worth more than memorizing any one of them.

**Try next:** implement a tiny key-value replica that stores a vector clock per key, and reproduce a write-write conflict that surfaces as two concurrent siblings. Once you can *cause* the conflict, the CAP-theorem discussions in later chapters stop being abstract.
