---
title: "Collaborative Text Editing: OT vs CRDT"
date: 2026-08-15
track: distributed-systems
summary: "Concurrent text editing reduces to one choice: transform concurrent operations against each other on a central server (operational transformation), or give every character a permanent identity so operations commute (sequence CRDT). This article covers the Jupiter/Wave lineage, an insert/insert transform, the tombstone and interleaving problems, and the Yjs measurements that answered the performance objection."
reading_time: 7
tags: [collaborative-editing, operational-transformation, crdt, yjs, google-docs, system-design]
sources:
  - title: "Nichols et al. — High-Latency, Low-Bandwidth Windowing in the Jupiter Collaboration System (UIST 1995)"
    url: "https://dl.acm.org/doi/10.1145/215585.215706"
  - title: "Google Wave Operational Transformation whitepaper"
    url: "https://svn.apache.org/repos/asf/incubator/wave/whitepapers/operational-transform/operational-transform.html"
  - title: "Kleppmann, Gomes, Beresford, Mulligan — Interleaving Anomalies in Collaborative Text Editors (PaPoC 2019)"
    url: "https://martin.kleppmann.com/papers/interleaving-anomalies.pdf"
  - title: "Joseph Gentle — I was wrong. CRDTs are the future"
    url: "https://josephg.com/blog/crdts-are-the-future/"
  - title: "dmonad/crdt-benchmarks — CRDT benchmark suite (Yjs, Automerge, and friends)"
    url: "https://github.com/dmonad/crdt-benchmarks"
---

**Gist.** A text insertion is expressed as a position, and a position is only meaningful in the document state that produced it, so replaying a concurrent remote operation verbatim corrupts it. Two families of solution exist: **operational transformation (OT)**, which rewrites the incoming operation's indices against the operations it did not see, and **sequence conflict-free replicated data types (CRDTs)**, which replace indices with permanent per-character identifiers so operations commute. OT pays with a central serialisation point and a transform function for every pair of operation types; CRDTs pay with per-character metadata and tombstones that persist after deletion.

Consider two replicas of the document `"abc"`. Alice inserts `"x"` at position 1; concurrently Bob inserts `"y"` at position 2. Applying each remote operation as received yields `"axybc"` on one replica and `"axbyc"` on the other. Both families target the same two properties: **convergence** — replicas that have observed the same set of operations display the same document — and **intention preservation** — Bob's `"y"` still lands between the characters he saw as `b` and `c`, wherever those characters have since moved.

## OT: transform the operation, keep the positions

OT keeps operations position-based and repairs them on arrival. Before a concurrent remote operation is applied, it is **transformed** against the local operations its author had not seen, shifting indices so the intention survives. For two insertions the rule is a comparison on position, with a site identifier breaking the tie so that **both replicas choose the same order for insertions at the same index**.

The correctness condition **TP1** states that the two application orders converge: `apply(apply(S, a), T(b, a)) == apply(apply(S, b), T(a, b))`. TP1 is provable for a single transform pair. Three or more concurrent sites additionally require **TP2**, the condition that transforming an operation against two concurrent operations gives the same result in either order. TP2 proved hard enough that **several published peer-to-peer OT algorithms were subsequently shown to be incorrect**.

Production OT avoids TP2 rather than satisfying it, following the **Jupiter** system (Nichols et al., UIST 1995): every client communicates only with the server, **each client-server connection maintains its own two-party transformation state**, and the server's arrival order is canonical. The pairwise reduction is what removes the need for TP2. Google Wave documented this architecture in its OT whitepaper — clients buffer local operations, keep **one batch in flight at a time**, and transform incoming server operations against the pending buffer — and it is the lineage behind Google Docs.

Two costs follow from the architecture. The server is mandatory, so there is no offline peer-to-peer merge, and it **serialises every edit for a document**, which bounds throughput per document rather than per cluster. Separately, the transform matrix has an entry per ordered pair of operation types, so it grows quadratically in the number of operation types: insert and delete over plain text is small, while rich-text attributes, tables and embedded objects enlarge the case analysis substantially.

### Implementation sketch (Scala)

The load-bearing part of OT is the transform function, not the transport. The insert/insert case:

```scala
final case class Ins(pos: Int, text: String, site: Int)

/** Transform `a` so it can be applied after concurrent `b` has been applied. */
def transformII(a: Ins, b: Ins): Ins =
  if a.pos < b.pos || (a.pos == b.pos && a.site < b.site) then a
  else a.copy(pos = a.pos + b.text.length)

/** Jupiter client: local ops are buffered until the server acknowledges them. */
final class ClientState(var doc: String, var pending: Vector[Ins]):

  def applyLocal(op: Ins): Unit =
    doc = doc.patch(op.pos, op.text, 0)
    pending = pending :+ op

  /** A remote op arrives already ordered by the server; it must be transformed
    * past every unacknowledged local op, and those ops rewritten in turn. */
  def applyRemote(remote: Ins): Unit =
    val (shifted, rebased) =
      pending.foldLeft((remote, Vector.empty[Ins])) { case ((r, acc), local) =>
        (transformII(r, local), acc :+ transformII(local, r))
      }
    doc = doc.patch(shifted.pos, shifted.text, 0)
    pending = rebased

  // ... an acknowledgement from the server drops the matching prefix of `pending`
```

Removing the `a.site < b.site` clause leaves insertions at an equal position ordered by whichever operation arrived first on each replica, which differs between replicas: the two documents diverge with no error raised.

## Sequence CRDTs: give every character an identity

Sequence CRDTs make the operation context-free. Instead of "insert at index 1", the operation reads "insert between the character with identifier `(alice,17)` and the character with identifier `(bob,4)`". Every character carries a **globally unique, permanent identifier**, and the merge rule orders concurrent insertions at the same location deterministically on every replica. Operations therefore **commute**: there is nothing to transform and no serialisation point. The general CRDT foundations — semilattices, G-Counters, OR-Sets — are covered in [the CRDT article](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication); text requires the *sequence* variants.

Two designs dominate. **RGA** (Roh et al., 2011) is a linked list of characters with an insert-after-identifier operation and concurrent siblings ordered by timestamp; **Automerge** builds on that shape. **YATA**, the algorithm inside **Yjs**, records **both a left and a right origin pointer per insertion**, which rules out conflicts in which two concurrent insertion ranges cross.

Two weaknesses are intrinsic to the approach.

- **Tombstones.** A delete cannot physically remove a character, because some replica may still name it as an insertion origin; the character is only marked dead. **Internal state therefore grows with edit history rather than with visible document length.** Yjs mitigates this by merging runs of consecutive characters into single items and garbage-collecting deleted content; Automerge retains full history by design, since it also serves as version control.
- **Interleaving.** Kleppmann et al. (PaPoC 2019) showed that position-number CRDTs such as Logoot and LSEQ can interleave two users' concurrent insertion runs character by character: Alice types "Alice" and Bob types "Bob" at the same location, and a replica renders a character-level mixture of the two. RGA and YATA-family designs avoid the worst form; **RGA retains a milder single-character anomaly**.

Performance was the historical objection to CRDTs, and published measurements no longer support it. Joseph Gentle reports that an early Automerge needed memory orders of magnitude larger than the document it held, whereas Yjs handles the same editing trace — roughly 260,000 edit operations recorded while an academic paper was written — in a few hundred kilobytes of encoded state and single-digit megabytes of memory, applying it in seconds; his later Rust implementation reaches millions of edits per second. The `crdt-benchmarks` suite, and specifically its B4 real-world trace, is the reference workload.

## Comparison

| | OT (Jupiter-style) | Sequence CRDT (RGA/YATA) |
|---|---|---|
| Ordering authority | Central server serialises operations | None — operations commute |
| Correctness burden | Transform functions per operation-type pair (TP1/TP2) | Identifier ordering plus merge rule, proved once |
| Offline / peer-to-peer | Poor — the server mediates everything | Native; syncs over any transport |
| Metadata overhead | Operations are small; document state is plain text | Per-character identifiers plus tombstones |
| Rich text / trees | Transform matrix grows quadratically in operation types | Yjs and Automerge ship map, array and XML types |
| Known failure mode | Incorrect transform yields silent divergence | Interleaving (Logoot/LSEQ), state growth |
| Shipping examples | Google Docs/Wave, ShareDB, Etherpad | Yjs, Automerge, Figma's multiplayer (CRDT-inspired) |

## Selection criteria

OT fits a deployment that already requires an authoritative server and a rich-text model with a mature library such as ShareDB: server-side ordering supplies a single place to enforce permissions and record history, and per-edit state is small. A sequence CRDT fits offline-first editing, peer-to-peer or end-to-end-encrypted synchronisation, and deployments where the server should not be an ordering authority — metadata overhead is traded for commutativity. A common contemporary answer is Yjs over a WebSocket relay in which the server acts as a publish-subscribe room and performs no transformation, with tombstone garbage collection and snapshot compaction as the operational follow-ups.

## Pitfalls

- Omitting the site-identifier tie-break in an insert/insert transform makes insertions at equal positions order by arrival, so replicas diverge silently with no exception and no checksum mismatch until a user notices scrambled text.
- Sending a second batch of local operations before the server acknowledges the first breaks the Jupiter assumption of one batch in flight per connection; the server transforms against a state the client has already moved past.
- Treating TP1 as sufficient in a peer-to-peer OT deployment reproduces the class of errors for which several published peer-to-peer OT algorithms were later shown incorrect: three concurrent sites, not two, expose it.
- Measuring CRDT memory on a freshly typed document hides tombstone growth, because internal state tracks edit history rather than visible length; a document that has been heavily edited and trimmed carries state far larger than its rendered text.
- Choosing a position-number CRDT such as Logoot or LSEQ exposes the interleaving anomaly documented by Kleppmann et al.: two users typing words concurrently at the same location can produce a character-level mixture of both.
- Deleting content does not reclaim space until garbage collection runs, so a sync payload sized from visible document length underestimates what is transferred.
