---
title: "Calvin: Deterministic Transaction Scheduling Instead of Two-Phase Commit"
date: 2026-08-27
track: distributed-systems
summary: Calvin agrees on a global order of transaction *inputs* before executing anything, so every replica deterministically reaches the same state and a cross-shard commit needs no two-phase commit round. Covers the sequencer/scheduler split, 10 ms epoch batching, the deterministic lock manager, and the price — dependent transactions require a reconnaissance read (OLLP), and interactive sessions do not fit the model at all.
reading_time: 8
tags:
- calvin
- deterministic-databases
- two-phase-commit
- transactions
- replication
- sharding
sources:
- title: Thomson, Diamond, Weng, Ren, Shao & Abadi — Calvin, Fast Distributed Transactions for Partitioned Database Systems (SIGMOD 2012)
  url: http://cs.yale.edu/homes/thomson/publications/calvin-sigmod12.pdf
- title: Daniel Abadi — It's Time to Move on from Two Phase Commit (DBMS Musings, 2019)
  url: http://dbmsmusings.blogspot.com/2019/01/its-time-to-move-on-from-two-phase.html
---

**Gist.** A transaction that spans shards normally ends with two-phase commit (2PC): every participant holds its locks through multiple network round-trips while the coordinator collects votes, and a coordinator crash between the vote and the decision leaves participants blocked. Calvin (Thomson et al., SIGMOD 2012) removes the agreement protocol by moving agreement *before* execution: all replicas first agree on a global order of transaction **inputs**, then execute them under a deterministic locking discipline that guarantees every replica reaches the same state — so no end-of-transaction vote is needed. The price is that a transaction's full read/write set must be known before it runs, which forces dependent transactions through an extra reconnaissance read and rules out interactive sessions.

## Why 2PC is expensive: the contention footprint

In a System R*-style distributed database, a multi-shard transaction cannot release its locks until the commit protocol finishes, because until the final decision arrives any participant might still be forced to abort — by a node failure, a deadlock victim selection, or a local integrity check. The paper names the total duration a transaction holds its locks, agreement protocol included, its **contention footprint**. The network round-trips of 2PC can exceed the time spent executing the transaction's actual logic, and when a few hot records appear in many distributed transactions, the extra lock-hold time on those records depresses whole-system throughput. Abadi's 2019 analysis adds the availability failure: a coordinator that crashes after participants vote "yes" but before it broadcasts the decision leaves them unable to either commit or abort until it recovers.

The root cause is **nondeterminism at commit time**. Because a participant may abort for reasons the others cannot predict (its own failure, its own deadlock), everyone must be asked. Calvin's design question is therefore: what must change so that no node can ever be surprised into aborting?

## Agree on inputs, not on outcomes

Calvin's answer is to replicate **batches of transaction requests** rather than database state or effects. Time is divided into **10-millisecond epochs**. During each epoch, the sequencer component on every machine collects the transaction requests that arrived at it; at the epoch boundary it compiles them into a batch and replicates that batch — either asynchronously from a master replica, or synchronously via Paxos (the published implementation uses ZooKeeper for the Paxos mode). Each scheduler then assembles its view of the global order by interleaving all sequencers' batches for the epoch in a fixed **deterministic round-robin**.

Replicating inputs alone is not sufficient: two replicas fed the same input stream may still execute it in different serial-equivalent orders (thread scheduling, network latency) and diverge. The second half of the guarantee is a **deterministic lock manager** in the scheduling layer. It resembles strict two-phase locking with two added invariants:

- If transactions A and B both want an exclusive lock on record R and A precedes B in the sequencer's order, **A must request its lock on R before B does**. Calvin enforces this by serializing all lock requests in a single thread that scans the serial order and requests, for each transaction, every lock it will ever need.
- Locks are **granted strictly in request order**: B cannot acquire R until A has acquired it, run to completion, and released it.

Under these rules every replica emulates the same serial order, so replicas cannot diverge — and deadlock is impossible, because lock acquisition follows one global order. **Aborts caused by the system itself — failures, deadlock victims — are eliminated by construction**; only deterministic, data-dependent aborts remain (a transaction whose logic says "abort if stock is zero" aborts identically on every replica). With no unpredictable aborts, a multi-shard transaction needs no vote. Each participating node executes its slice and waits only for **one-way messages** carrying remote read results; once it has them, it commits its part unilaterally. Node failure does not force an abort either: a crashed node's work can be re-read from another replica of the same partition or replayed from the logged input, so the survivors proceed.

## The three layers and the five-phase execution

Calvin is a layer above a storage engine exposing basic create/read/update/delete (CRUD) operations, split into a **sequencing layer** (global input order plus its replication and logging), a **scheduling layer** (the deterministic lock manager and a pool of execution threads), and a **storage layer**. All three scale horizontally; each node runs one partition of each layer, so there is no single point of failure.

Once a worker thread holds all of a transaction's locks, execution proceeds in five phases:

1. **Read/write set analysis** — classify the declared read and write sets into locally stored and remote elements; nodes storing write-set elements are *active participants*, nodes storing only read-set elements are *passive*.
2. **Perform local reads.**
3. **Serve remote reads** — forward local read results to the worker threads on every active participant.
4. **Collect remote read results** — active participants gather what they need (passive participants are already done after phase 3).
5. **Execute and apply writes** — run the transaction logic, applying local writes and ignoring non-local ones, which the counterpart thread on the owning node applies.

Note what is absent: no prepare message, no vote, no coordinator. The messages in phases 3–4 are the only cross-node traffic per transaction. On 4-core EC2 machines the authors measured about 500 ms average transaction latency with Paxos across three data centers versus roughly 100 ms within one — but because the sequencing step happens before any lock is acquired, replication mode changed **latency only**: total transactional throughput was unaffected.

### Deterministic locking sketch (Scala)

The load-bearing component is small enough to state directly — a single-threaded granter that walks the agreed order:

```scala
final case class Txn(id: Long, readSet: Set[String], writeSet: Set[String])

final class DeterministicLockManager {
  private val holders = collection.mutable.Map.empty[String, List[Long]] // FIFO queues

  /** Called by ONE thread, in exact sequencer order — this is the invariant. */
  def requestAll(t: Txn): Unit = {
    for (key <- (t.readSet ++ t.writeSet).toList.sorted)
      holders(key) = holders.getOrElse(key, Nil) :+ t.id
  }

  def ready(t: Txn): Boolean = // runnable once it heads every queue it sits in
    (t.readSet ++ t.writeSet).forall(k => holders(k).headOption.contains(t.id))

  def release(t: Txn): Unit =
    for (key <- t.readSet ++ t.writeSet)
      holders(key) = holders(key).filterNot(_ == t.id)
}
```

Because `requestAll` is invoked in sequencer order by one thread, no two replicas can grant conflicting locks in different orders, and no cycle can form in the waits-for graph. Concurrency comes from the pool of worker threads executing every transaction that is currently `ready` — the granter is serial, execution is not.

## The price: reconnaissance reads and no interactive sessions

The lock manager's requirement that **every transaction declare its complete read/write set before execution begins** is where the model's costs concentrate.

A transaction whose write set depends on data it has not yet read — the paper calls these **dependent transactions** — cannot declare that set up front. The canonical case is a secondary-index lookup: "update the order whose customer name is X" cannot name its record keys until the index has been consulted. Calvin handles these with **optimistic lock location prediction (OLLP)**: the client first issues an inexpensive, low-isolation, unreplicated, read-only **reconnaissance query** that performs the reads needed to discover the read/write set, then submits the actual transaction — annotated with that predicted set — into the global sequence. Because the underlying records may change between reconnaissance and execution, the transaction **re-checks its reconnoitered reads under its locks and deterministically restarts if the predicted set is stale**. The authors observe that indexes on volatile fields are uncommon in practice (one indexes stock *symbol*, not stock *price*), so repeated restarts are rare; TPC-C's Payment transaction, a dependent transaction whose index is never modified by the workload, never restarts.

The harder exclusion is structural. Transaction logic is submitted as a self-contained function (C++ in the published implementation) over the CRUD interface — the whole transaction enters the sequencer as one input. A session that holds a transaction open across client think time — `BEGIN`, a query, application-side logic, another query, `COMMIT`, as ordinary JDBC (Java Database Connectivity) code does — has no single input to sequence: its future reads and writes depend on responses the client has not seen yet. **Interactive, conversational transactions therefore do not fit Calvin's model**; the application must be rewritten as one-shot stored procedures. This is the model's real adoption cost, and it is a schema-of-the-application cost, not a tuning parameter.

Determinism constrains the storage layer too. Because logging and concurrency control refer only to logical record keys, physical techniques that lean on page-level state — ARIES-style physiological logging, next-key locking for phantom protection — cannot be implemented as-is; recovery instead replays logged inputs from a checkpoint.

## Pitfalls

- **Sequencer latency is user-visible latency.** Every transaction waits for its epoch boundary plus input replication before a single lock is acquired; with Paxos across three data centers the paper measured ~500 ms average — throughput is untouched, but a latency-sensitive workload feels the full sequencing delay.
- **A stale reconnaissance read causes a deterministic restart, not a silent skip.** If records read by the OLLP query change before execution, the transaction re-enters the sequencer with a new read/write set; a hot secondary index on a volatile field turns this into a restart loop.
- **A slow transaction stalls the order behind it on that partition.** Locks are granted strictly in sequence order, so a transaction blocked on a disk read holds up every conflicting successor; Calvin mitigates this by prefetching cold records into memory (with an artificial delay) *before* the transaction enters the scheduler, keeping disk latency out of the contention footprint.
- **Data-dependent aborts must live inside the transaction logic.** Any abort condition expressed outside the sequenced function (a client-side cancel, an external timeout) would break replica agreement; only conditions computable from the database state may abort.
- **Porting an interactive application is a rewrite, not a migration.** ORM-generated conversational transactions have no representation in the model; each must become a one-shot procedure with a declarable read/write set.
