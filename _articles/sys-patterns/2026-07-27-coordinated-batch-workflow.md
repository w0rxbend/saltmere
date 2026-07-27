---
title: "Coordinated Batch Workflows: Composing Copier, Sharder, and Reduce"
date: 2026-07-27
track: sys-patterns
summary: "Brendan Burns' batch building blocks — copier, filter, splitter, sharder, merge, and reduce — composed into a split→process→reduce pipeline, mapped onto Argo Workflows DAGs and indexed Kubernetes Jobs."
reading_time: 6
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

Brendan Burns' *Designing Distributed Systems* (2nd ed., 2024) splits batch work across three chapters: **work queues**, **event-driven batch processing**, and **coordinated batch processing**. The work-queue pattern — a source, a shared queue, and horizontally-scaled stateless workers — I covered in an earlier article on this track, so I won't rehearse it here. This one is about the other two: the small, single-purpose containers you *compose* into a multi-stage pipeline, and how that composition lands on a real runtime.

## The building blocks

Event-driven and coordinated batch processing give you six reusable stages. Each is a container that does one thing to a stream and hands the result to the next stage.

| Pattern | Purpose |
|---|---|
| **Copier** | Duplicates one input stream into N identical streams so independent consumers can each process every event. |
| **Filter** | Drops events that don't match a predicate; only the survivors flow downstream. |
| **Splitter** | Fans one event into several related events (e.g. one order → one event per line item) for parallel handling — no dropping, unlike a filter. |
| **Sharder** | Routes each event to a partition by a key (hash, range) so a fixed set of workers each own a disjoint slice. |
| **Merge / join** | The inverse of copier/splitter: gathers multiple streams back into one. A **barrier** join waits for all upstream shards to finish before the next stage starts. |
| **Reduce** | Aggregates the merged results into a summary — count, sum, histogram — collapsing many records into a final value. |

The mental model: **copier** and **splitter** widen the pipeline, **filter** narrows it, **sharder** partitions it, **merge/join** re-collects it, and **reduce** collapses it. A real pipeline is these stages wired in sequence.

## Work-queue vs. event-driven vs. coordinated

The three are chosen by coupling, not by scale. A **work queue** suits embarrassingly-parallel, independent items where any worker can take any task and order doesn't matter. **Event-driven** batch processing chains transform stages (copier/filter/splitter/sharder/merge) where each event flows through a topology, typically over a pub/sub bus like Kafka. **Coordinated** batch processing adds synchronization — a join/barrier and a reduce — for when the *final* answer depends on every shard completing. When your job is "split the input, process each partition, then aggregate," you're in coordinated territory, and that maps cleanly onto a DAG.

## A split→process→reduce DAG in Argo

Argo Workflows expresses stage dependencies with the `dependencies` field: a task runs only after every task it lists has completed. That is exactly a barrier/join. Here is a three-stage pipeline — split, three parallel shard-processors, then reduce:

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

Mapping each stage to a pattern:

- **`split`** is the **splitter** — it partitions the input into shard files or queue keys.
- **`process-0..2`** are the **sharder + worker** stage. Each task owns one key range; because they only depend on `split`, Argo runs them in parallel.
- **`reduce`** is the **join/barrier + reduce**. Its `dependencies: [process-0, process-1, process-2]` is the barrier — it cannot start until all three shards finish — and its container performs the aggregation.

## Same shape as an indexed Kubernetes Job

If the process stage is uniform, you don't need three hand-written DAG tasks; a single **indexed Job** is the native primitive. With `completionMode: Indexed`, Kubernetes runs `completions` pods and injects a unique `JOB_COMPLETION_INDEX` (0..N-1) into each, giving you static work assignment — a sharder without a router:

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

The Job succeeds only when all indices complete — an implicit barrier. In practice you wrap it: an Argo DAG where `split → indexed-Job → reduce`, letting the Job own the fan-out and the DAG own the join and reduce. Copier and filter stages, being pure stream transforms with no barrier, are better left to the event-driven bus (Kafka consumers) upstream of the DAG rather than modeled as Job dependencies.

**Try next:** Deploy the diamond example from the Argo DAG walk-through into a kind cluster, then swap the two middle tasks for the indexed Job above and confirm the reduce stage still blocks on the barrier by watching `kubectl get pods -w`.
