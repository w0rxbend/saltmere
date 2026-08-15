---
title: "Coordinated Batch Workflows: Composing Copier, Sharder, and Reduce"
date: 2026-07-27
track: sys-patterns
summary: "Brendan Burns' batch building blocks — copier, filter, splitter, sharder, merge, and reduce — composed into a split→process→reduce pipeline, mapped onto Argo Workflows DAGs and indexed Kubernetes Jobs."
reading_time: 7
tags: [batch, workflow, kubernetes, argo, patterns]
sources:
  - title: "Designing Distributed Systems, 2nd Edition (Brendan Burns, O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Designing Distributed Systems (free 1st-edition ebook, Microsoft)"
    url: "https://info.microsoft.com/rs/157-GQE-382/images/EN-CNTNT-eBook-DesigningDistributedSystems.pdf"
  - title: "Argo Workflows — DAG walk-through"
    url: "https://argo-workflows.readthedocs.io/en/latest/walk-through/dag/"
  - title: "Kubernetes — Jobs"
    url: "https://kubernetes.io/docs/concepts/workloads/controllers/job/"
  - title: "Kubernetes — Indexed Job for Parallel Processing with Static Work Assignment"
    url: "https://kubernetes.io/docs/tasks/job/indexed-parallel-processing-static/"
---

**Gist.** A batch computation whose final answer depends on every partition cannot be expressed as a pool of independent workers, because nothing in that pool knows when the last partition is done. Coordinated batch processing solves this by composing single-purpose stages — split, shard, process, join, reduce — into a directed acyclic graph (DAG) in which the reduce stage declares a dependency on every processing stage, and that dependency edge *is* the barrier. The cost is that the pipeline now has a slowest-shard critical path and a failure domain the size of the whole graph: one unrecoverable shard leaves the reduce stage permanently unstartable.

Brendan Burns' *Designing Distributed Systems* (2nd ed., 2024) splits batch work across three chapters: **work queues**, **event-driven batch processing**, and **coordinated batch processing**. The work-queue pattern — a source, a shared queue, and horizontally scaled stateless workers — is covered in an earlier article on this track. This article addresses the other two: the small single-purpose containers composed into a multi-stage pipeline, and how that composition lands on a real runtime.

## The building blocks

Event-driven and coordinated batch processing supply six reusable stages. Each is a container that performs one operation on a stream and hands the result to the next stage.

| Pattern | Purpose |
|---|---|
| **Copier** | Duplicates one input stream into N identical streams so independent consumers can each process every event. |
| **Filter** | Drops events that do not match a predicate; only the survivors flow downstream. |
| **Splitter** | Routes each event to one or more output streams according to a criterion — unlike a filter, nothing is dropped; every event reaches at least one downstream branch. |
| **Sharder** | Routes each event to a partition by a key (hash, range) so a fixed set of workers each own a disjoint slice. |
| **Merge / join** | The inverse of copier/splitter: gathers multiple streams back into one. A **barrier** join waits for all upstream shards to finish before the next stage starts. |
| **Reduce** | Aggregates the merged results into a summary — count, sum, histogram — collapsing many records into a final value. |

The structural summary: **copier** and **splitter** widen the pipeline, **filter** narrows it, **sharder** partitions it, **merge/join** re-collects it, and **reduce** collapses it. A real pipeline is these stages wired in sequence.

## Work queue, event-driven, coordinated

The three patterns are distinguished by coupling rather than by scale. A **work queue** suits embarrassingly parallel, independent items where any worker can take any task and order does not matter. **Event-driven** batch processing chains transform stages (copier, filter, splitter, sharder, merge) so that each event flows through a topology, typically over a publish/subscribe bus such as Kafka; **no stage in that chain observes global completion**, only the events that reach it. **Coordinated** batch processing adds synchronization — a join barrier followed by a reduce — for the case where the final answer depends on every shard completing. That requirement is what forces a DAG: the barrier is an edge that cannot exist in a flat queue topology.

The invariant the coordinated form maintains is narrow and worth stating exactly: **the reduce stage observes either the complete set of shard outputs or nothing at all.** It never observes a proper subset. Every property below follows from preserving that invariant.

## A split→process→reduce DAG in Argo

Argo Workflows expresses stage dependencies with the `dependencies` field: a task runs only after every task it names has completed successfully. That is a barrier. The pipeline below has three stages — split, three parallel shard processors, then reduce:

{% raw %}
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: split-process-reduce-
spec:
  entrypoint: pipeline
  templates:
  - name: pipeline
    dag:
      tasks:
      - name: split                 # splitter: chunk input into 3 shards
        template: splitter
      - name: process-0             # sharder + worker: owns shard 0
        dependencies: [split]
        template: worker
        arguments: {parameters: [{name: shard, value: "0"}]}
      - name: process-1
        dependencies: [split]
        template: worker
        arguments: {parameters: [{name: shard, value: "1"}]}
      - name: process-2
        dependencies: [split]
        template: worker
        arguments: {parameters: [{name: shard, value: "2"}]}
      - name: reduce                 # join barrier + reduce
        dependencies: [process-0, process-1, process-2]
        template: reducer
  - name: splitter
    container: {image: pipeline/splitter:1.0, command: [/split]}
  - name: worker
    inputs: {parameters: [{name: shard}]}
    container:
      image: pipeline/worker:1.0
      command: [/process, "--shard={{inputs.parameters.shard}}"]
  - name: reducer
    container: {image: pipeline/reducer:1.0, command: [/reduce]}
```
{% endraw %}

Mapping each stage to a pattern:

- **`split`** is the **splitter**: it partitions the input into shard files or queue keys.
- **`process-0..2`** are the **sharder plus worker** stage. Each task owns one key range; because each depends only on `split`, the three run concurrently.
- **`reduce`** is the **join barrier plus reduce**. Its `dependencies: [process-0, process-1, process-2]` is the barrier — it cannot start until all three shard tasks have completed — and its container performs the aggregation.

The failure mode is a direct consequence: **a task whose dependencies did not all complete is never started, and the workflow terminates without a result.** There is no partial reduce. Recovery therefore has to happen inside a shard task (retry, resume from a checkpoint) or by resubmitting the workflow; the DAG offers no way to reduce over the shards that did succeed.

## The same shape as an indexed Kubernetes Job

Where the processing stage is uniform, three hand-written DAG tasks are unnecessary; a single **indexed Job** is the native primitive. With `completionMode: Indexed`, Kubernetes runs `completions` pods and gives each a distinct completion index in the range 0 to `completions - 1`, exposed to the container as the `JOB_COMPLETION_INDEX` environment variable. That is **static work assignment: a sharder with no router**, since the partition a pod owns is determined by its index rather than by a dispatch decision.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: shard-process
spec:
  completions: 3          # 3 shards total
  parallelism: 3          # all at once
  completionMode: Indexed # each pod gets JOB_COMPLETION_INDEX
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: worker
        image: pipeline/worker:1.0
        command: ["/process", "--shard=$(JOB_COMPLETION_INDEX)"]
```

An indexed Job completes successfully only when **there is one successfully completed pod for every index**, which is the same barrier the DAG edge provides, expressed by the Job controller instead. The two knobs are independent: `completions` fixes the number of shards and hence the partitioning of the data, while `parallelism` caps how many shards run at once. Setting `parallelism` below `completions` does not change the result, only the wall-clock schedule — the remaining indices start as earlier pods finish.

A practical composition uses both: an Argo DAG of `split → indexed Job → reduce`, with the Job owning the fan-out and the DAG owning the join and the reduce. Copier and filter stages are pure stream transforms with no barrier, so they fit the event-driven bus (Kafka consumers) upstream of the DAG rather than being modelled as Job dependencies.

### Implementation sketch (Scala)

The barrier is what distinguishes this pattern, and it is small enough to state directly. The load-bearing property is that the combinator yields a reduced value only when **every** shard future has succeeded, and fails as a whole otherwise — mirroring the DAG edge and the indexed Job's completion rule.

```scala
import scala.concurrent.{ExecutionContext, Future}

final case class ShardResult(index: Int, count: Long, sum: Long)

// One worker per shard index: the static assignment an indexed Job performs.
def processShard(index: Int): Future[ShardResult] = ???

def splitProcessReduce(shards: Int)(using ExecutionContext): Future[ShardResult] =
  val fanOut: Seq[Future[ShardResult]] = (0 until shards).map(processShard)

  // Future.sequence is the join barrier: it completes with all results, or
  // fails with the first failure and never yields a partial collection.
  val barrier: Future[Seq[ShardResult]] = Future.sequence(fanOut)

  barrier.map { results =>
    require(results.map(_.index).toSet == (0 until shards).toSet)
    results.reduce((a, b) => ShardResult(-1, a.count + b.count, a.sum + b.sum))
  }
```

`Future.sequence` short-circuits on the first failure while the remaining shards keep running; nothing cancels them, so the resource cost of a doomed fan-out is paid in full. The two runtimes differ here rather than matching the combinator: a failed Argo DAG task does not itself stop sibling tasks that are already running, whereas a Kubernetes Job that has exhausted its `backoffLimit` is marked failed and its remaining active pods are terminated by the Job controller.

## Pitfalls

- **Reduce receives a partial answer because the barrier was expressed as a timeout.** Waiting a fixed interval instead of on the dependency edge lets a slow shard be silently excluded, producing an aggregate that is wrong rather than absent.
- **A shard task listed no dependency on `split` and read an incomplete input.** In Argo a task with an empty `dependencies` list starts immediately; the ordering constraint exists only where the edge is declared.
- **Changing `completions` on a rerun repartitions the data.** The completion index determines which slice a pod owns, so a shard count that differs between runs assigns different records to the same index, invalidating any per-index checkpoint from the earlier run.
- **`parallelism` is mistaken for the shard count.** Lowering it schedules the same `completions` indices in successive waves; the partitioning is unchanged and the job takes longer rather than producing fewer shards.
- **A non-idempotent worker corrupts its shard on retry.** A pod that fails midway is replaced with the same index, so any output already written for that index is written a second time unless the worker overwrites rather than appends.
- **Copier and filter stages are modelled as DAG nodes.** Neither has a completion condition tied to the whole stream, so expressing them as tasks forces an artificial end-of-input boundary that the event-driven bus does not have.
