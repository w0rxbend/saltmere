---
title: "CAP and PACELC: the trade-off is C-vs-A only during a partition"
date: 2026-08-10
track: distributed-systems
summary: "CAP is not 'pick 2 of 3.' Partitions are not optional, so the only real choice CAP forces is consistency vs availability, and only while a partition is happening. PACELC fixes CAP's blind spot — the latency-vs-consistency trade-off you pay in the normal case. Here are precise definitions, Brewer's own walk-back, real system classifications, and how to answer 'is this CP or AP?' without hand-waving."
reading_time: 6
tags: [cap-theorem, pacelc, consistency, availability, linearizability, abadi]
sources:
  - title: "Gilbert & Lynch, Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services (SIGACT News 2002)"
    url: "https://www.comp.nus.edu.sg/~gilbert/pubs/BrewersConjecture-SigAct.pdf"
  - title: "Brewer, CAP Twelve Years Later: How the 'Rules' Have Changed (IEEE Computer, 2012)"
    url: "https://ieeexplore.ieee.org/document/6133253/"
  - title: "Abadi, Consistency Tradeoffs in Modern Distributed Database System Design (IEEE Computer, 2012)"
    url: "https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf"
  - title: "Abadi, Problems with CAP, and Yahoo's little known NoSQL system (DBMS Musings, 2010)"
    url: "https://dbmsmusings.blogspot.com/2010/04/problems-with-cap-and-yahoos-little.html"
  - title: "PACELC design principle — Wikipedia"
    url: "https://en.wikipedia.org/wiki/PACELC_design_principle"
---

Almost everything said about CAP in interviews is subtly wrong. "You can only have two of three" is a slogan that survives because it rhymes, not because it's accurate. Let me state the theorem precisely, kill the misconception, and then show the framework that actually captures the trade-off most systems make in production — PACELC.

## The three properties, defined precisely

Gilbert and Lynch's 2002 paper gives CAP its only rigorous form. The letters are not vague qualities; they are specific guarantees:

- **Consistency (C)** here means **linearizability** — atomic consistency. There is a single, total order of operations consistent with real time; a read returns the value of the most recent completed write, as if the whole system were one register. This is *not* ACID's "C" (integrity constraints). Same letter, different concept.
- **Availability (A)** means **every request received by a non-failing node must terminate in a (non-error) response.** A node that is up but refuses to answer because it can't reach its peers is, by this definition, not available.
- **Partition tolerance (P)** means **the network may drop or delay arbitrarily many messages** between nodes and the system keeps operating. A partition is a split where two groups of nodes cannot talk to each other.

The theorem: no distributed system can simultaneously guarantee all three. The proof is a two-line argument. Split the nodes into `{G1}` and `{G2}` that can't communicate. Write `v2` to `G1`; then read from `G2`. If the read returns a response at all (A) it can only return the stale `v1`, because no message crossed the partition (P) — so it isn't linearizable (violates C). If instead you insist on C, `G2` must refuse or block the read (violates A). You cannot have both **while the partition holds**. That last clause is the whole game.

## The misconception: "pick 2 of 3"

Here is what the slogan gets wrong. **Partition tolerance is not a property you choose to drop.** Partitions are caused by the network — dropped packets, a failed switch, a GC pause that looks like a dead node — and you do not get a vote. Over any real network, partitions *will* happen. So "CA" (giving up P to keep C and A) is not a design point for a distributed system; it describes a single-node database, or a cluster that simply stops when the network breaks.

That means the genuine choice is binary and *conditional*: **when a partition occurs, do you sacrifice C or A?** That is the only decision CAP actually forces, and it only applies during the partition. This is why "CP" and "AP" are the meaningful labels and "CA" is essentially a category error.

## Brewer's own "12 years later" correction

In 2012 Brewer wrote *CAP Twelve Years Later* precisely to walk back the slogan he'd popularized. His clarifications:

1. **"Because partitions are rare, there is little reason to forfeit C or A when the system is not partitioned."** When there's no partition, you can have *both* strong consistency and full availability. CAP tells you nothing about normal operation.
2. **The 2-of-3 framing is misleading** because it treats the three as symmetric and always-on. In reality C and A are only traded against each other during a partition.
3. **C, A, and P are not binary.** Consistency and availability come in degrees and "can vary by subsystem or even by operation" — one endpoint can be strongly consistent while another is eventually consistent.
4. He reframes design around the **partition lifecycle**: *detect* the partition, *enter an explicit partition mode* that limits some operations, then *recover* — reconcile state and compensate for mistakes — once communication resumes.

So the modern reading of CAP is narrow: it is a statement about behavior *during* partitions, and nothing more.

## PACELC: the part CAP forgot

CAP's blind spot is that it says nothing about the common case, when the network is healthy. Yet fully-replicated systems make a trade-off *all the time*: to answer with the strongest consistency, a node must coordinate with other replicas, and coordination costs **latency**. Daniel Abadi's PACELC (2010 blog post, 2012 IEEE paper) captures both cases in one line:

> **if Partition (P) then choose Availability (A) or Consistency (C); Else (E) choose Latency (L) or Consistency (C).**

The `PAC` half is just CAP. The `ELC` half is the new, always-relevant content: even with no partition, a low-latency design must weaken consistency (fewer replicas on the critical path, async replication), and a strongly-consistent design must pay round-trips (synchronous quorums, leader coordination). This is the everyday tax that CAP ignored. Note that Abadi's "consistency" in PACELC is broadened to linearizability-style strong consistency generally, not only the atomic register of the proof.

## Classifying real systems

A system gets two letters: what it gives up under partition, and what it gives up otherwise.

| System | PACELC | Under partition | Normal operation | Why |
|---|---|---|---|---|
| Dynamo-style / Cassandra / Riak | **PA/EL** | keep Availability | keep Latency | Leaderless, sloppy quorums, async replication; tunable but *defaults* favor answering fast over agreeing |
| HBase / BigTable | **PC/EC** | keep Consistency | keep Consistency | Single-master per region/tablet; a partitioned region is unavailable rather than divergent |
| Spanner | **PC/EC** | keep Consistency | keep Consistency | Paxos + TrueTime give external consistency; commit-wait adds latency it refuses to trade away (Google argues partitions are rare enough to call it "effectively CA") |
| MongoDB | **PA/EC** | keep Availability | keep Consistency | Primary election on partition can keep serving; healthy reads default to the primary for consistency |
| PNUTS (Yahoo) | **PC/EL** | keep Consistency | keep Latency | Timeline consistency per record with a master; reads hit a local (possibly stale) replica for latency — the case that motivated PACELC |
| Single-node RDBMS (MySQL) | **PC/EC** | n/a (not distributed) | keep Consistency | No partition to tolerate; ACID by construction |

The `PA/EL` vs `PC/EC` split lines up with the leaderless-vs-leadered architectural fork. The interesting entries are the asymmetric ones — **PA/EC** (MongoDB) and **PC/EL** (PNUTS) — which are exactly the cases the two-letter CAP vocabulary cannot express, and the reason PACELC exists.

Two of these choices connect to machinery covered elsewhere in this journal. Where a system lands on the C/L axis is often just a quorum setting — see [quorum replication and why R + W > N is the whole game](/articles/distributed-systems/2026-07-25-quorum-replication-r-plus-w); Cassandra's tunable consistency is literally choosing `R` and `W` per request, which slides it along the `EL`↔`EC` spectrum. And "consistency" itself is a spectrum from linearizable down through causal to eventual — the consistency-models articles in the corpus unpack what you're actually weakening.

## Answering "is this CP or AP?" in an interview

Do not answer the label first. Answer the *mechanism*, then let the label fall out:

1. **Find the coordination point.** Where does a write become durable/visible — a single leader, a quorum, or any replica? That determines what happens when a node is cut off.
2. **Ask what a partitioned node does with a request.** Does it still answer (→ **AP**, risking staleness) or does it error/block until it can reach a quorum (→ **CP**, sacrificing availability)? That single behavior *is* the CAP classification.
3. **Then add the PACELC half:** in the healthy case, does it coordinate before answering (**EC**, higher latency) or answer locally and reconcile later (**EL**)?
4. **State it as a sentence, not a label:** "It's PA/EL — a partitioned replica still serves reads and writes with last-write-wins reconciliation, and even without a partition it answers from the nearest replica rather than waiting on a quorum." That sentence proves you understand the trade-off; "it's AP" proves you memorized a slogan.

And keep the caveat ready: real systems are **tunable and per-operation**. Cassandra with `QUORUM`/`QUORUM` behaves far more like CP than the same cluster at `ONE`/`ONE`. The honest answer names the default and the knob.

**Try next:** take a system you run and place it on the PACELC grid twice — once at its default settings, once at its strongest-consistency settings — and write the one-sentence justification for each cell. Then force a partition in a test cluster (drop traffic between two nodes with `iptables`) and observe which of C or A your reads and writes actually give up; compare that to the label you predicted.
