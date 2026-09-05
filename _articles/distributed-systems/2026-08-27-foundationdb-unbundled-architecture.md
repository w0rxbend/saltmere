---
title: "FoundationDB: An Unbundled Database — Sequencer, Resolvers, and Log Servers as Separate Roles"
date: 2026-08-27
track: distributed-systems
summary: "FoundationDB splits a transactional key-value store into single-purpose processes: a sequencer hands out versions, proxies orchestrate commits, resolvers run optimistic conflict checks over key ranges, and log servers make mutations durable before storage servers ever apply them. Each role scales and recovers independently; the price is that any failure in the transaction or log system triggers a full transaction-system reboot into a new epoch."
reading_time: 8
tags:
- foundationdb
- unbundled-architecture
- occ
- mvcc
- write-ahead-log
- transaction-recovery
sources:
- title: Zhou et al. — FoundationDB, A Distributed Unbundled Transactional Key Value Store (SIGMOD 2021)
  url: https://www.foundationdb.org/files/fdb-paper.pdf
- title: FoundationDB documentation — Architecture
  url: https://apple.github.io/foundationdb/architecture.html
---

**Gist.** Most databases bundle timestamp management, conflict detection, logging, and storage into one process, so all four scale together and fail together. FoundationDB (FDB) unbundles them: a **sequencer** issues versions, **commit proxies** orchestrate, **resolvers** run optimistic concurrency control (OCC) checks over key ranges, **log servers** persist the write-ahead log (WAL), and **storage servers** apply mutations asynchronously and serve reads. Each role is provisioned and scaled independently, and reads never touch the write path. The cost: FDB handles *every* transaction-system failure through a single recovery path — the whole transaction system shuts down and is rebuilt as a new generation, aborting in-flight transactions rather than masking the fault.

## Two planes, five roles

The SIGMOD 2021 paper (Zhou et al.) describes an FDB cluster as a **control plane** and a **data plane**. The control plane holds critical metadata: **coordinators** form a disk Paxos group and elect a singleton **cluster controller**, which monitors all servers and recruits the three transaction-management singletons — the sequencer, a data distributor, and a ratekeeper for overload protection.

The data plane divides into three sub-systems that map onto the classic bundled engine:

- **Transaction system (TS)** — the sequencer, the proxies, and the resolvers, **all stateless**. It performs in-memory transaction processing: version assignment, commit orchestration, and conflict detection.
- **Log system (LS)** — the log servers, which store the WAL for the transaction system as replicated, sharded persistent queues, one queue per storage server.
- **Storage system (SS)** — the storage servers, the vast majority of processes in the cluster. Each stores contiguous key ranges in a modified SQLite engine; together they form a distributed B-tree and serve all client reads.

The decoupling is the design principle the paper names *divide-and-conquer*: the write path (TS + LS) and the read path (SS) scale independently. Clients read directly from sharded storage servers, so read throughput scales linearly with the number of storage servers; write throughput scales by adding proxies, resolvers, and log servers. Multi-version concurrency control (MVCC) data lives in the storage servers, not the transaction system — which is what allows the TS to remain stateless.

## The commit pipeline

A client transaction starts by asking a proxy for a **read version**, a timestamp guaranteed to be no less than any commit version issued before the request. Reads then go straight to storage servers at that snapshot; writes are buffered client-side and the cluster is not contacted until commit. At commit, the client ships the read set and write set — both expressed as **key ranges** — to a commit proxy, and the pipeline runs in three steps:

1. **Sequencing.** The proxy asks the sequencer for a **commit version**, strictly larger than every existing read or commit version. The sequencer advances the version at a rate of **one million versions per second**, so versions double as coarse timestamps. Each commit version becomes the transaction's log sequence number (LSN), and the sequencer also returns the *previous* LSN, so downstream consumers can detect gaps and process commits in exact order.
2. **Resolution.** The proxy sends the transaction's conflict ranges to **range-partitioned resolvers**. Each resolver keeps `lastCommit`, an in-memory map from key range to the last commit version that modified it (a version-augmented probabilistic skip list in practice). A transaction aborts if any range it read was modified after its read version — the lock-free OCC check shown below. Because modified ranges expire after the **5-second MVCC window**, the map stays bounded. A microbenchmark in the paper shows one single-threaded resolver sustaining **280 K conflict checks per second**.
3. **Logging.** If every resolver admits the transaction, the proxy broadcasts the mutation to all log servers, tagged with the log servers preferred by the affected storage shards; the others receive an empty message body carrying only the LSN, preserving the gap-free sequence. The transaction is committed once all designated log servers have made it durable — **before any storage server applies it**. The proxy reports the committed version back to the sequencer and replies to the client.

Storage servers are not on the commit path at all. They aggressively pull redo logs from log servers — often before the logs are durable — and apply them in LSN order. On production clusters the paper measures the 99.9th percentile of the average storage lag at **3.96 ms**, so by the time a client's next read arrives, the data it committed is usually already visible. Because a storage server can apply semi-committed updates, it must be able to roll them back after a failure; the in-memory multi-version data makes that rollback a discard, not an undo pass.

### Implementation sketch (Scala)

The resolver's check is the paper's Algorithm 1: abort on read-write conflict, then record the write set.

```scala
final case class Range(begin: String, end: String) // [begin, end)

final class Resolver:
  // key range -> last commit version that modified it,
  // entries older than the 5 s MVCC window are expired
  private var lastCommit = Map.empty[Range, Long]

  private def intersecting(r: Range): Iterable[(Range, Long)] =
    lastCommit.filter((c, _) => c.begin < r.end && r.begin < c.end)

  def resolve(readSet: Seq[Range], writeSet: Seq[Range],
              readVersion: Long, commitVersion: Long): Boolean =
    val conflict = readSet.exists { r =>
      intersecting(r).exists((_, v) => v > readVersion)
    }
    if !conflict then
      writeSet.foreach(w => lastCommit = lastCommit.updated(w, commitVersion))
    !conflict
```

The key space is partitioned across resolvers so these checks run in parallel — but a transaction commits only if **all** resolvers admit it. An aborted transaction may already have been admitted by a subset of resolvers, which then record its write set anyway and can cause **false-positive conflicts** for later transactions. The paper reports this has not mattered in production: transactions' key ranges usually land on one resolver, false positives expire with the 5-second window, and the observed conflict rate in Apple's multi-tenant workloads is **below 1%**.

## Recovery: make failure a common case

Unbundling pays off most visibly in failure handling. FDB does not mask transaction-system faults with quorums; the paper's stated principle is to handle *all* TS and LS failures through one recovery path. The sequencer monitors the proxies, resolvers, and log servers, and **terminates itself if any of them fails**. The cluster controller detects the dead sequencer and recruits a new one, which reads the previous transaction-system configuration from the coordinators, stops the old log servers, and recruits a fresh set of proxies, resolvers, and log servers. Transaction processing is thereby divided into **epochs**, each a generation of the transaction system with its own sequencer.

Because proxies and resolvers are stateless, their recovery is trivial — nothing to replay. The only real work is deciding where the old log ends. Each proxy piggybacks its **known committed version (KCV)** — the highest LSN it has fully replicated — on every log write; each log server tracks the maximum KCV it has seen and its own **durable version (DV)**. The new sequencer collects these and computes the **recovery version (RV)** as the minimum DV over a quorum of old log servers: everything at or below RV is durable everywhere required, everything above it is discarded, and storage servers roll back any in-memory versions beyond RV. There is no checkpoint and no redo/undo replay during recovery, because storage servers were already continuously applying the log — redo processing *is* the normal forward path.

The cost is visible to clients. Any in-flight commit caught by a recovery returns `commit_result_unknown` — the transaction may or may not have committed, which is why FDB requires transactions to be **idempotent** under retry. The paper reports that in Apple's production clusters the total time to detect a failure, shut down the transaction system, and recover is **usually under five seconds**; the architecture documentation puts the typical recovery itself at a few hundred milliseconds. A single log-server disk failure therefore reboots the entire write path — a trade the design accepts in exchange for one well-tested recovery code path and f-failure tolerance with only **f + 1** log replicas rather than the 2f + 1 a quorum scheme needs.

## Pitfalls

- A transaction whose reads span more than 5 seconds fails with `transaction_too_old`, because resolvers and storage servers discard MVCC state beyond the window.
- Non-idempotent transactions corrupt data under retry: `commit_result_unknown` after a recovery means the commit may already be durable.
- A read-hot key set does not conflict, but a write-hot *range* does — conflict detection operates on key ranges, so a range clear conflicts with every read inside it.
- Spreading a transaction's key ranges across multiple resolvers admits partial-abort bookkeeping: a subset of resolvers records the write set of a transaction another resolver rejected, inflating the conflict rate until the window expires.
- Adding storage servers raises read throughput but not commit throughput; the write path is bounded by proxies, resolvers, and log servers, which must be scaled separately.
