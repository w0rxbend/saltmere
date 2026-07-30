---
title: "Chain Replication and CRAQ: strong consistency that still scales reads"
date: 2026-07-30
track: distributed-systems
summary: "Chain replication gives you linearizable writes with a dead-simple failure model — but the tail node becomes a read bottleneck. CRAQ fixes that by letting every node serve reads with a clean/dirty version check. Here's how both work, and a small Python sketch."
reading_time: 6
tags: [chain-replication, craq, replication, linearizability, consistency, quorum]
sources:
  - title: "Chain Replication for Supporting High Throughput and Availability (OSDI 2004) — van Renesse & Schneider"
    url: "https://www.usenix.org/legacy/event/osdi04/tech/full_papers/renesse/renesse.pdf"
  - title: "Object Storage on CRAQ: High-throughput chain replication for read-mostly workloads (USENIX ATC 2009) — Terrace & Freedman"
    url: "https://www.cs.princeton.edu/courses/archive/fall19/cos418/papers/craq.pdf"
  - title: "MIT 6.824 Lecture 9 — CRAQ (notes)"
    url: "https://timilearning.com/posts/mit-6.824/lecture-9-craq/"
  - title: "go-craq — a Go implementation of CRAQ"
    url: "https://github.com/despreston/go-craq"
---

Most replication schemes make you trade consistency against throughput. Chain replication is interesting because its *write* path is both linearizable and cheap to reason about — no consensus round per operation, no quorum arithmetic. The catch is the read path, which is where CRAQ comes in.

## The chain

Arrange the *R* replicas of an object in a total order: a **head**, some middle nodes, and a **tail**. Writes and reads enter at fixed ends.

- A **write** always goes to the **head**. The head applies it locally, then forwards it down the chain. Each node applies and forwards to its successor. When the write reaches the **tail**, the tail applies it and sends an **ack** back up the chain.
- A **read** in plain chain replication is served *only by the tail*.

That's the whole protocol, and its beauty is the invariant it produces: the tail has applied a write if and only if *every* node has applied it. So the tail's state is exactly the set of committed writes. A read at the tail therefore returns the latest committed value — **linearizable** — with no coordination at all. Compare that to quorum systems (covered in the R+W article here), where a read must contact multiple replicas and reconcile.

Failure handling is unusually simple too, because the order is fixed:

- **Head fails:** its successor becomes the new head. In-flight writes the old head hadn't forwarded are simply lost (they were never acked).
- **Tail fails:** its predecessor becomes the new tail. Since the predecessor has everything the tail had *plus possibly a few more* not-yet-acked writes, those extra writes are now considered committed — safe.
- **Middle node fails:** its predecessor is reconnected to its successor; a short reconciliation replays any writes the successor missed.

A separate, fault-tolerant **master** (typically Paxos/Raft-backed — see the Paxos article here) monitors liveness and publishes the current chain membership. The master handles metadata consensus; the chain handles the high-volume data path. That split is the point.

## Why the tail is a problem

Every read hits one node. Double your read traffic and the tail is your ceiling — the other *R−1* replicas sit there holding identical data they're not allowed to serve. For a read-mostly workload (the common case for object stores, caches, config) that's most of your hardware idle.

## CRAQ: let every node serve reads

**CRAQ** (Chain Replication with Apportioned Queries, Terrace & Freedman 2009) keeps the exact write path but makes reads serve-able from *any* node — head, tail, or middle — without losing linearizability. The trick is versioning.

Each node stores, per object, possibly **multiple versions**, each tagged **clean** or **dirty**:

- When a node receives a new write via propagation, it appends that version and marks it **dirty** — it's been seen but not yet known-committed.
- When the tail commits the write and the ack propagates back up, each node marks that version **clean** and drops older versions.

Now a read at any node:

1. If the newest local version is **clean**, return it immediately. No coordination.
2. If it's **dirty**, the node doesn't guess. It asks the **tail** one tiny question — "what's the latest committed version number for this object?" — and returns *that* version from its own local store.

That version query to the tail is cheap (just a version number, not the object), and it only happens for objects with a write in flight. On a read-mostly workload almost every read hits case 1 and is served locally. You've turned *R−1* idle replicas into read capacity while keeping the same strong guarantee: a read never returns a value newer than what's committed, and never an older one than a previously-returned committed value.

CRAQ also supports weaker modes if you want them — eventual-consistency reads that return the latest local (possibly dirty) version with no tail query, or bounded-staleness reads — but the default is the strong one, and it's the interesting one.

## A sketch

A node's read logic is the whole idea in a dozen lines:

```python
class Node:
    def __init__(self, is_tail, tail_client):
        self.versions = {}      # key -> list[(version, value, clean: bool)]
        self.is_tail = is_tail
        self.tail = tail_client

    def read(self, key):
        vs = self.versions[key]
        latest = vs[-1]
        version, value, clean = latest
        if clean or self.is_tail:
            return value                       # local, no coordination
        # dirty: ask the tail which version is committed
        committed_v = self.tail.latest_committed_version(key)
        for v, val, _ in vs:
            if v == committed_v:
                return val                     # serve that version locally

    def on_propagate(self, key, version, value):   # write coming down the chain
        self.versions.setdefault(key, []).append((version, value, False))  # dirty
        if self.is_tail:
            self.commit(key, version)          # tail commits, then acks upstream

    def on_ack(self, key, version):            # ack coming back up the chain
        self.versions[key] = [
            (v, val, (v == version) or clean)  # mark committed version clean
            for (v, val, clean) in self.versions[key] if v >= version
        ]
```

Real implementations (see `go-craq`) add the master, chain reconfiguration, and per-key chains so hot and cold objects don't share a bottleneck — but the read/write asymmetry above is the core.

## When to reach for it

Chain replication + CRAQ shines for **read-mostly, strongly-consistent** stores where you'd otherwise pay quorum-read latency: metadata services, session stores, config, feature-flag backends, small object stores. It's a poor fit for write-heavy or geo-distributed-write workloads, where the serial chain adds write latency proportional to its length and a distant tail hurts.

**Try next:** Implement the `Node` above with three in-process nodes wired as a chain, then write a test that fires a `write` and, *before* the ack propagates back, issues a read at the middle node — assert it performs a version query to the tail and returns the committed value, not the dirty one. Then extend it: make reads on a clean object assert that *no* tail query happened, proving reads really are local in the common case.
