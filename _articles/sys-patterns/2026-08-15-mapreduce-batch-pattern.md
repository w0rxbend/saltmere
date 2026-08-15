---
title: "MapReduce: the batch pattern that built Google, and why it retired"
date: 2026-08-15
track: sys-patterns
summary: "The OSDI '04 MapReduce paper reduced distributed batch jobs to two functions and made fault tolerance the framework's problem: re-execute tasks, run backups for stragglers, commit atomically. Twenty years on, Google itself dumped it, Spark and Beam generalized it into DAGs, and a single machine running DuckDB now handles workloads that once justified a cluster. The ideas survived; the rigid two-phase shape did not."
reading_time: 7
tags: [mapreduce, batch-processing, spark, duckdb, fault-tolerance, google-papers]
sources:
  - title: "Dean & Ghemawat — MapReduce: Simplified Data Processing on Large Clusters (OSDI '04, paper PDF)"
    url: "https://research.google.com/archive/mapreduce-osdi04.pdf"
  - title: "USENIX OSDI '04 — MapReduce presentation page"
    url: "https://www.usenix.org/conference/osdi-04/mapreduce-simplified-data-processing-large-clusters"
  - title: "Data Center Knowledge — Google Dumps MapReduce in Favor of New Hyper-Scale Analytics System (Cloud Dataflow, 2014)"
    url: "https://www.datacenterknowledge.com/hyperscalers/google-dumps-mapreduce-in-favor-of-new-hyper-scale-analytics-system"
  - title: "Chambers et al. — FlumeJava: Easy, Efficient Data-Parallel Pipelines (PLDI 2010)"
    url: "https://research.google/pubs/flumejava-easy-efficient-data-parallel-pipelines/"
  - title: "Jordan Tigani — Big Data is Dead (MotherDuck)"
    url: "https://motherduck.com/blog/big-data-is-dead/"
---

**Gist.** Distributing a batch computation across thousands of unreliable commodity machines requires partitioning, scheduling, failure recovery and inter-machine transport, and writing that machinery per job is prohibitive. MapReduce (Dean & Ghemawat, OSDI '04) removes the machinery from the application by restricting the application: **the program supplies two deterministic, side-effect-free functions, `map` and `reduce`, and the framework owns everything else**, recovering from failure by re-executing tasks rather than checkpointing state. The cost of that bargain is expressive power — every computation must be bent into a fixed map→shuffle→reduce shape, with the intermediate result materialised to disk between stages.

The paper's task-granularity discussion gives a representative production shape: **M = 200,000 map tasks and R = 5,000 reduce tasks across 2,000 worker machines**. The scale reflects 2004 hardware — the benchmark cluster the paper reports on used roughly 1,800 machines, each with two 2 GHz Xeons, 4 GB of memory and two 160 GB IDE disks.

## The pattern: map, shuffle, reduce

The programming model is a distributed group-by:

- **Map** takes an input record and emits intermediate `(key, value)` pairs.
- **Shuffle** — the framework's half — partitions intermediate pairs by `hash(key) mod R`, so every pair sharing a key lands at the same reducer, sorted by key.
- **Reduce** receives `(key, iterator-of-values)` and folds them into output.

```text
map(String doc_name, String contents):
    for word in contents:
        EmitIntermediate(word, "1")

reduce(String word, Iterator values):
    result = 0
    for v in values: result += ParseInt(v)
    Emit(AsString(result))
```

The partition function is the load-bearing invariant: **all values for a key are delivered to exactly one reduce task**, which is what makes a reducer's view of a key complete and therefore what makes a fold correct. It is also the source of skew, since a key's entire value set is bounded by one machine's capacity.

Two refinements in the paper matter beyond word count. A **combiner** applies the reduce function to a mapper's local output before the shuffle; where maps emit heavy repetition, thousands of `("the", 1)` pairs collapse to one, cutting shuffle bandwidth by orders of magnitude. The combiner is admissible only when the reduce function is associative and commutative over its value type, because the framework may apply it zero, one or many times. **Backup tasks** address stragglers: near the end of a job the master schedules duplicate executions of the remaining in-progress tasks and takes whichever copy finishes first. A single slow disk otherwise holds the whole job hostage — the paper's sort benchmark ran **44% slower** with backup tasks disabled.

## Fault tolerance by re-execution

The central claim is that deterministic, side-effect-free tasks make recovery a matter of re-running work rather than restoring state. The master pings workers; when one stops responding, **its completed map tasks are re-executed elsewhere, because map output lives on the failed machine's local disk**, while completed reduce output is already durable in the distributed file system and needs no replay. Reducers write to a temporary file and **atomically rename it on completion**, so a task that dies mid-write leaves no partially visible output and a duplicate execution resolves to a single committed file.

The state machine per task is therefore small: idle → in-progress (assigned to a worker) → completed, with an in-progress task returning to idle on worker failure and a completed *map* task also returning to idle when its host dies. In one sort-benchmark variant the paper deliberately kills 200 of the 1,746 worker processes partway through; the scheduler re-ran the lost work and the job still completed. Re-execution only remains correct because the model **forbids arbitrary cross-task communication**: a task has no observable effect other than its output file, so running it twice is indistinguishable from running it once. Spark's lineage-based recovery is the direct descendant of this property.

### Implementation sketch (Scala)

The shuffle is the part worth making legible: partitioning by key hash, grouping, and applying a combiner that must be associative.

```scala
type Pair[K, V] = (K, V)

/** One mapper's output, pre-aggregated locally by the combiner. */
def mapSide[K, V](
    records: Iterator[String],
    mapFn: String => IterableOnce[Pair[K, V]],
    combine: (V, V) => V,          // must be associative and commutative
    partitions: Int
): Map[Int, Map[K, V]] =
  records
    .flatMap(mapFn(_).iterator)
    .foldLeft(Map.empty[Int, Map[K, V]]) { case (acc, (k, v)) =>
      val p = math.floorMod(k.hashCode, partitions)   // hash(key) mod R
      val bucket = acc.getOrElse(p, Map.empty[K, V])
      acc.updated(p, bucket.updated(k, bucket.get(k).fold(v)(combine(_, v))))
    }

/** Reduce task p: merge every mapper's bucket p, then fold per key. */
def reduceSide[K, V](
    shards: Seq[Map[Int, Map[K, V]]],
    p: Int,
    combine: (V, V) => V
): Seq[Pair[K, V]] =
  shards
    .flatMap(_.getOrElse(p, Map.empty))
    .groupMapReduce(_._1)(_._2)(combine)   // key -> folded value
    .toSeq
    .sortBy(_._1.toString)                 // reducers see keys in sorted order
```

Applying `combine` on both sides is what the combiner contract permits; a non-associative fold would produce different results depending on how many mappers happened to see a key.

## Why Google retired it

By 2014 Urs Hölzle stated at Google I/O that Google no longer used MapReduce — retired internally "years ago" in favour of what became Cloud Dataflow. The reasons reported are structural rather than defects in the implementation:

- **Real pipelines are directed acyclic graphs (DAGs), not one map and one reduce.** Production jobs chained dozens of MapReduce stages, materialising every intermediate result to disk. FlumeJava (PLDI 2010) expresses a pipeline as composable parallel collections, lets an optimiser fuse operations, and compiles down to a smaller set of MapReduce executions.
- **Batch only.** Continuous computation had to be approximated with scheduled small batches; Dataflow and Beam unified batch and streaming under one model of windows and watermarks (see [stream processing windows and watermarks](/articles/sys-patterns/2026-08-13-stream-processing-windows-watermarks)).
- **Disk between every stage.** Spark keeps working sets in cluster memory across stages; Flink uses pipelined streaming execution. Both retain MapReduce's core contracts — partition by key, deterministic re-execution, moving code to data — while discarding the fixed two-phase shape.

The pattern was absorbed rather than abandoned. Every `groupByKey` in Spark, every `GroupByKey` in Beam, and every shuffle in a distributed SQL join is the shuffle described in the paper.

## When one machine beats the cluster

Hardware moved as well. Jordan Tigani's *Big Data is Dead* argues most organisations never had big data: across the BigQuery workloads he cites, **the large majority of queries processed under 100 MB**, and storage sizes clustered far below a terabyte even among paying customers. A single cloud instance can now be rented with tens of cores and hundreds of gigabytes of memory, with no network between them. The cluster-scale word count becomes:

```sql
-- DuckDB, one process, no cluster
SELECT word, count(*) AS n
FROM (SELECT unnest(string_split(lower(content), ' ')) AS word
      FROM read_text('docs/*.txt'))
GROUP BY word ORDER BY n DESC;
```

or, on an existing cluster, in PySpark:

```python
spark.read.text("docs/*.txt") \
     .selectExpr("explode(split(lower(value), ' ')) AS word") \
     .groupBy("word").count().orderBy("count", ascending=False)
```

| | MapReduce (2004) | Spark/Flink/Beam | DuckDB on one box |
|---|---|---|---|
| Model | fixed map→shuffle→reduce | DAG of operators | SQL, vectorized |
| Intermediates | GFS/local disk | memory, spill | memory, spill |
| Fault tolerance | re-execute tasks | lineage / checkpoints | rerun the query |
| Sweet spot | cluster-scale batch, 2004 hardware | data larger than one machine | working set that fits one machine |
| Ops cost | enormous | significant | `pip install duckdb` |

Distribution buys throughput and pays for it in coordination, shuffle input/output and stragglers — the problems the paper spends much of its length mitigating. Where the working set fits one machine's disk and the query fits its memory plus spill, the cluster contributes overhead only. The distributed engine earns its cost when data volume, ingest rate or isolation requirements exceed a single machine.

## Pitfalls

- **A non-associative or non-commutative combiner silently changes results.** The framework may apply the combiner any number of times, so a fold such as "first value wins" produces output that depends on mapper boundaries rather than on the input.
- **One hot key serialises the job.** Since all values for a key go to a single reduce task, a skewed key distribution leaves R − 1 reducers idle while one machine processes the bulk of the data; adding reducers does not help.
- **Non-deterministic map output breaks re-execution.** If a task's output depends on wall-clock time, a random seed or an external service, the re-run after a worker failure produces different intermediate data, and the guarantee that duplicate execution is unobservable no longer holds.
- **Side effects outside the output file escape the atomic rename.** A task that writes to an external store during execution applies that write once per attempt, so backup tasks and failure re-execution duplicate it.
- **Map output is not durable.** Losing a worker after its map tasks completed still forces re-execution of those tasks, because the intermediate files lived on that machine's local disk.
- **Chaining stages multiplies disk traffic.** Each MapReduce in a chain materialises its full intermediate result, so a pipeline of dozens of stages spends most of its wall-clock time writing and re-reading data no consumer ever inspects.
