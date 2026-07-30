---
title: "Session guarantees: the four promises that keep a user's own timeline straight"
date: 2026-07-30
track: distributed-systems
summary: "Data-centric consistency argues about what all clients see together. Session guarantees flip the question: within one user's session, the system must never appear to run backwards. Here are the four guarantees from the Bayou paper, why they matter, and how MongoDB, Cassandra, and DynamoDB actually deliver (or quietly break) them."
reading_time: 6
tags: [consistency, session-guarantees, causal-consistency, replication, mongodb, cassandra]
sources:
  - title: "Session Guarantees for Weakly Consistent Replicated Data (Terry, Demers, Petersen, Spreitzer, Theimer, Welch — PDIS 1994)"
    url: "https://pages.cs.wisc.edu/~remzi/Classes/739/Fall2016/Papers/bayou-sessions94.pdf"
  - title: "van Steen & Tanenbaum, Distributed Systems (4th ed.) — free PDF"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "MongoDB Docs — Causal Consistency and Read and Write Concerns"
    url: "https://www.mongodb.com/docs/manual/core/causal-consistency-read-write-concerns/"
  - title: "Jepsen — Consistency Models"
    url: "https://jepsen.io/consistency/models"
  - title: "CASSANDRA-2494 — Quorum reads are not monotonically consistent"
    url: "https://issues.apache.org/jira/browse/CASSANDRA-2494"
---

Data-centric consistency models (linearizability, sequential, causal) argue about what *all* clients see *together* — one global agreement everyone shares. That's expensive, and often more than a single user needs. **Client-centric consistency** asks a narrower, cheaper question: within *one* client's ongoing **session**, does the system ever appear to run backwards? A user who posts a comment and then reloads to find it gone, or refreshes a feed that loses items it just showed, experiences a system that violates causality *in their own timeline* — even if the global state is perfectly fine. The four **session guarantees** from the 1994 Bayou paper by Terry et al. are exactly the promises that prevent this.

## The setup: replicas that disagree, and a client that moves

Assume a weakly-consistent replicated store: many servers, writes propagate lazily (gossip, anti-entropy), and any two replicas may hold different subsets of writes at any instant. A client is not pinned to one replica — a load balancer may route request 1 to replica A and request 2 to replica B. That mobility is where the anomalies come from.

The Bayou model gives each write a globally unique **WID** (write identifier) and tracks two sets per session:

- the **write-set** — WIDs of writes this session performed;
- the **read-set** — WIDs of writes *relevant* to this session's reads.

`DB(S,t)` denotes the set of writes server `S` has applied at time `t`. The guarantees are constraints relating these sets. In practice you don't ship WID sets around — you summarize them with a **version vector** (a `<server, logical-clock>` map), which is the compact form the paper proposes.

## The four guarantees

| Guarantee | Plain-English promise | Formal constraint (Bayou) |
|---|---|---|
| **Read Your Writes** | You always see your own past writes. | If read `R` follows write `W` in the session, then `W ∈ DB(S,t)` when `R` runs on `S`. |
| **Monotonic Reads** | Reads never lose data a previous read showed. | If `R1` precedes `R2`, then `RelevantWrites(R1) ⊆ DB(S2,t2)` when `R2` runs. |
| **Monotonic Writes** | Your writes apply in the order you issued them. | If `W1` precedes `W2` in the session, every server that has `W2` also has `W1`, ordered `W1 → W2`. |
| **Writes Follow Reads** | A write lands *after* the writes it was based on. | If `R1` precedes `W2`, then any server holding `W2` also holds the writes `R1` read, ordered before `W2`. |

Two are about **reads not regressing** (RYW, MR), two are about **write ordering being respected** (MW, WFR). Concrete failures each one rules out:

- **Read Your Writes** — You change your email in settings, the confirmation page reads from a stale replica, and it still shows the old address. RYW forbids this.
- **Monotonic Reads** — You scroll a timeline (replica A, has 100 posts), the next page hits replica B (has 60), and 40 posts vanish. MR forbids a read from seeing *fewer* writes than an earlier read.
- **Monotonic Writes** — You save a document, then save again; a replica applies save-2 but not save-1, so the "latest" version is missing your first edit's content. MW forbids reordering your own writes.
- **Writes Follow Reads** — You read a comment and reply to it; the reply propagates to a replica that doesn't yet have the original comment, so people see the reply before the thing it answers. WFR preserves the read→write causal edge. This is the guarantee that makes threaded discussions coherent.

Stack all four within a session and you get **causal consistency** as observed by that one client. That's not a coincidence — it's precisely how MongoDB frames its causally-consistent sessions.

## Why "client-centric" is cheaper than "data-centric"

The key difference: session guarantees never require replicas to *agree with each other*. They only constrain what *this client* is allowed to observe, given what it has already observed. You can enforce them entirely at the client (or a session-sticky proxy) by carrying a little state. Van Steen & Tanenbaum present them this way in the consistency-and-replication chapter — as a family of guarantees defined from the client's viewpoint rather than the data's.

The mechanism, in ~20 lines: the client keeps a version vector summarizing writes it must not fall behind, sends it with each request, and a replica must be **caught up to** that vector before serving.

```python
class Session:
    def __init__(self):
        self.read_vv  = {}   # summarizes RelevantWrites for reads (MR, WFR)
        self.write_vv = {}   # summarizes this session's writes  (RYW, MW)

    @staticmethod
    def _merge(a, b):
        return {s: max(a.get(s, 0), b.get(s, 0)) for s in a | b.keys()}

    def read(self, key, replicas):
        # MR + RYW: need a replica that has seen everything we depend on
        need = self._merge(self.read_vv, self.write_vv)
        r = pick_replica_dominating(replicas, need)   # else block / redirect
        val, val_vv = r.read(key)
        self.read_vv = self._merge(self.read_vv, val_vv)   # never regress
        return val

    def write(self, key, val, replicas):
        # WFR: the new write must causally follow what we've read
        dep = self._merge(self.write_vv, self.read_vv)
        r = pick_replica_dominating(replicas, dep)
        wid_vv = r.write(key, val, depends_on=dep)         # MW via dep on prior writes
        self.write_vv = self._merge(self.write_vv, wid_vv)
        return wid_vv
```

`pick_replica_dominating` is the crux: a replica may serve only if its `DB` vector **dominates** the required vector. If none does, you wait, trigger anti-entropy, or fall back to a stronger read. **Sticky sessions** are the degenerate optimization of this: pin the client to one replica and RYW/MR come almost for free, because that replica's `DB` only grows.

## How real systems map onto this

- **Bayou / session tokens.** The original design: the client holds the version vectors above; any server can serve a request as long as it dominates the token. Move servers freely, guarantees hold.
- **MongoDB causal consistency.** A causally-consistent session tags reads with `afterClusterTime` and advances an `operationTime`/cluster time on every reply — the same version-vector idea, specialized to a hybrid logical clock. The docs list exactly these four guarantees, but with a sharp caveat: you only get *all four with durability* when reads use `readConcern: "majority"` **and** writes use `writeConcern: "majority"`. Weaker concerns silently drop guarantees.
- **DynamoDB.** Eventually-consistent reads give none of these across replicas; a *strongly* consistent read gives read-your-writes for that item. There's no cross-request session token, so you get RYW per strongly-consistent read, not automatic monotonic reads across a mobile session — you carry that yourself.
- **Cassandra `LOCAL_QUORUM`.** The classic trap: people assume `R + W > RF` (e.g. quorum reads and writes) buys monotonic reads. It doesn't by itself. **CASSANDRA-2494** documents the scenario — a value written at `ONE` can be returned by a quorum read *before* read-repair is acknowledged on a second replica, so a later read can go backwards. It was fixed (1.0) by making the coordinator wait for a read-repair ack before returning. The lesson stands: quorum intersection guarantees you *can* see the latest write, not that successive reads are *monotonic*.

Session guarantees are the cheapest consistency that still feels correct to a human. If you only ship one thing, ship **read-your-writes + monotonic reads** — they kill the two anomalies users notice fastest.

**Try next:** Take the `Session` class above and build three in-memory replicas that gossip at random intervals. Write a value through the session, then force the *next* read to a replica that hasn't received the gossip yet. First confirm the naive version (no version vectors) returns stale data — a read-your-writes violation — then wire in `pick_replica_dominating` and assert the read either blocks or redirects until a replica dominates your `write_vv`. Finally, add a monotonic-reads test: read from a fresh replica, then from a *staler* one, and prove the guard rejects the regression.
