---
title: "Collaborative Text Editing: OT vs CRDT"
date: 2026-08-15
track: distributed-systems
summary: "The Google-Docs interview question reduces to one choice: transform concurrent operations against each other on a central server (OT), or give every character a permanent identity so operations commute (CRDT). Here's the Jupiter/Wave lineage, an insert/insert transform in ten lines, the tombstone and interleaving problems, and the Yjs numbers that killed the 'CRDTs are too slow' objection."
reading_time: 6
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

Two users have the document `"abc"`. Alice inserts `"x"` at position 1; concurrently Bob inserts `"y"` at position 2. If each side naively applies the other's operation as-received, Alice ends with `"axybc"` and Bob with `"axbyc"` — positions are *context-dependent*, so replaying an operation in a different context corrupts it. Every collaborative editor is an answer to this one problem, and the two answers are **Operational Transformation** and **sequence CRDTs**.

Both aim for the same correctness properties: **convergence** (all replicas that have seen the same operations show the same document) and **intention preservation** (Bob's `"y"` still lands between what he saw as `b` and `c`, wherever those characters moved).

## OT: transform the operation, keep the positions

OT keeps operations position-based and fixes them up on arrival. Before applying a concurrent remote operation, you **transform** it against the local operations it didn't see, shifting indices so the intention survives:

```python
# a is the op to transform; b is a concurrent op already applied.
# site ids break the tie so both sides pick the same order.
def transform_ii(a: Ins, b: Ins) -> Ins:
    if a.pos < b.pos or (a.pos == b.pos and a.site < b.site):
        return a                            # b landed after a: no shift
    return Ins(a.pos + len(b.text), a.text, a.site)  # slide right past b
```

The correctness condition (**TP1**) says the two orders converge: `apply(apply(S, a), T(b, a)) == apply(apply(S, b), T(a, b))`. That's provable for one transform pair. The trouble starts with three or more concurrent sites, which needs a second property (TP2) that turned out to be so hard that several published peer-to-peer OT algorithms were later shown incorrect.

Production OT sidesteps TP2 with a **central server**, following the **Jupiter** system (Nichols et al., UIST 1995): every client talks only to the server, each connection maintains its own 2-party transformation state, and the server's arrival order is the canonical order. This is the architecture Google Wave documented in its OT whitepaper — clients buffer local ops, send one batch in flight at a time, and transform incoming server ops against the pending buffer — and it's the lineage behind Google Docs. The costs: the server is mandatory (no offline peer-to-peer merge) and it serializes every edit for a document, which is why a viral Docs link degrades into view-only mode. The transform-function matrix also grows quadratically with operation types — insert/delete text is fine; rich-text attributes, tables, and embedded objects make the case analysis genuinely painful.

## CRDTs: give every character an identity

Sequence CRDTs make the operation itself context-free: instead of "insert at index 1," you say "insert between the character with ID `(alice,17)` and the character with ID `(bob,4)`." Every character gets a globally unique, permanent ID, and the merge rules are designed so concurrent inserts at the same spot are ordered deterministically on every replica — operations **commute**, so there's nothing to transform and no server required. (The general CRDT foundations — semilattices, G-Counters, OR-Sets — are covered in [the CRDT article](/articles/distributed-systems/2026-07-26-crdts-conflict-free-replication); text needs the *sequence* flavor.)

The designs that matter in interviews: **RGA** (Roh et al., 2011) — a linked list of characters, insert-after-ID, concurrent siblings ordered by timestamp — which is roughly what **Automerge** builds on; and **YATA**, the algorithm inside **Yjs**, which keeps left *and* right origin pointers per insertion to rule out crossing conflicts.

Two classic weaknesses:

- **Tombstones.** A delete can't physically remove a character — some replica may still reference it as an insertion origin — so it's only marked dead. The document's internal state grows with edit history, not visible length. Yjs mitigates by merging runs of consecutive characters into single items and garbage-collecting deleted content; Automerge deliberately keeps full history (it doubles as version control).
- **Interleaving.** Kleppmann et al. (PaPoC 2019) showed that position-number CRDTs like Logoot and LSEQ can interleave two users' concurrent insertions character-by-character — Alice types "Alice", Bob types "Bob" at the same spot, and a replica renders `"BAliceob"`-style garbage. RGA/YATA-family designs avoid the worst of this (RGA retains a milder single-character anomaly), which is one reason Yjs and Automerge won.

**Performance** was the historical objection, and it's dead. Joseph Gentle's writeup has the numbers: circa-2019 Automerge needed over a gigabyte of memory for a 100 KB academic paper, while modern Yjs stores that same ~260k-keystroke editing trace in about 160 KB on disk and ~3 MB in memory, applying it in seconds; his Rust implementation hits millions of edits per second. The `crdt-benchmarks` suite (the B4 real-world trace) is the standard receipt to cite.

## Comparison

| | OT (Jupiter-style) | Sequence CRDT (RGA/YATA) |
|---|---|---|
| Ordering authority | Central server serializes ops | None — ops commute |
| Correctness burden | Transform functions per op-type pair (TP1/TP2) | ID ordering + merge rule, provable once |
| Offline / P2P | Poor — server mediates everything | Native; sync over any transport |
| Metadata overhead | Ops are small; doc state is plain text | Per-character IDs + tombstones |
| Rich text / trees | Painful (transform matrix explodes) | Yjs/Automerge ship maps, arrays, XML types |
| Known failure smell | Wrong transform ⇒ silent divergence | Interleaving (Logoot/LSEQ), state bloat |
| Shipping examples | Google Docs/Wave, ShareDB, Etherpad | Yjs (Notion-style editors), Automerge, Figma's multiplayer (CRDT-inspired) |

## Which to pick in the interview

Pick **OT** when you were going to run an authoritative server anyway and the document model is rich text with a mature library (ShareDB): server-side ordering also gives you a clean permission and history story, and per-edit state is tiny. Pick a **CRDT** when you need offline-first editing, peer-to-peer or end-to-end-encrypted sync, or server-optional scaling — you trade metadata overhead for commutativity. The strong default answer in 2026: "Yjs on a WebSocket relay; the server is just a dumb pub-sub room, which removes the OT server bottleneck" — then mention tombstone GC and snapshot compaction as the operational follow-ups.

**Try next:** implement `transform_ii` plus the insert/delete case, drive it with two scripted clients through a toy server, and delete the tie-break clause to watch the replicas silently diverge.
