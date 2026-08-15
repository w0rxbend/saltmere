---
title: "Serving ML models: the same replication and sharding patterns, new constraints"
date: 2026-07-30
track: sys-patterns
summary: "Burns' serving patterns — replicated stateless services, sharded services — apply directly to model inference, but the constraints flip: models are large, GPUs are scarce and expensive, and the largest models no longer fit on one machine. How replicas, sharding, and multi-node inference map onto AI serving, with a KServe example."
reading_time: 6
tags: [ai-infrastructure, model-serving, sharding, replication, kserve, gpu]
sources:
  - title: "Designing Distributed Systems (2nd ed.) — Brendan Burns (serving patterns; AI infrastructure)"
    url: "https://www.oreilly.com/library/view/designing-distributed-systems/9781098156343/"
  - title: "Announcing KServe v0.15: Advancing Generative AI Model Serving — CNCF blog (June 2025)"
    url: "https://www.cncf.io/blog/2025/06/18/announcing-kserve-v0-15-advancing-generative-ai-model-serving/"
  - title: "KServe — Multi-node/Multi-GPU Inference documentation"
    url: "https://kserve.github.io/website/docs/model-serving/generative-inference/multi-node"
  - title: "vLLM: Easy, Fast, and Cheap LLM Serving with PagedAttention"
    url: "https://blog.vllm.ai/2023/06/20/vllm.html"
---

**Gist.** Model inference is a serving workload, so Brendan Burns' two serving patterns in *Designing Distributed Systems* — replicate a stateless service behind a load balancer, shard it when the state exceeds one replica — carry over unchanged in form. What changes is the unit being replicated: a replica pins model weights into graphics processing unit (GPU) memory, a resource that is scarce, expensive per hour, and slow to warm. The cost imposed is that the replicate-versus-shard decision is no longer cheap to get wrong: sharding a model adds interconnect traffic to **every** inference, and replicating a model that does not fit does not schedule at all.

## Replication: the base case, gated by GPUs

An inference server that loads a model into memory and answers requests is stateless in Burns' sense: each request is independent of every other, so throughput scales by running *N* identical replicas behind a load balancer. The routing layer needs no knowledge of request content, and any replica can serve any request. That is the whole invariant, and it holds for model inference exactly as it holds for a stateless web tier.

The unit of replication is where the arithmetic diverges. A web replica occupies a few hundred megabytes of host RAM on a central processing unit (CPU) that can be rented in bulk. A model replica pins a multi-gigabyte weight file into GPU memory, and admission to the replica set is gated on a whole accelerator being free. Startup is not instantaneous either: pulling a container image of many gigabytes and loading weights into device memory takes minutes rather than the sub-second start of a web pod. Two consequences follow.

- **Scale-to-zero carries more weight than it does for web services.** An idle web pod wastes a negligible amount; an idle GPU pod holds an expensive accelerator out of the schedulable pool for as long as it runs. KServe supports scaling an inference service to zero replicas when idle.
- **Autoscaling on requests per second (RPS) mismodels the load.** Inference latency is dominated by batch size and sequence length rather than by request count, so a fixed RPS threshold does not correspond to a fixed level of saturation. GPU utilisation or queue depth tracks the real constraint. Within a replica, batching requests together — dynamic or continuous batching, the scheduling technique vLLM applies alongside its paged key-value (KV) cache — raises throughput before an additional replica is warranted.

## Sharding: when the model does not fit on one machine

Burns' sharded-service pattern applies when the state exceeds a single replica's capacity: the state is partitioned, and each request is routed to the shard that owns the relevant data. The canonical example is a cache larger than one node's RAM. The defining property is that **a request touches exactly one shard**, which is what keeps the pattern horizontally scalable — adding shards adds capacity without adding per-request work.

Large language models (LLMs) reach the same wall for a different reason: the weights exceed one GPU's memory, or one machine's aggregate GPU memory. The parameter count multiplied by the bytes per parameter gives the floor — a 400-billion-parameter model in half precision is roughly 800 GB of weights alone, before activations and KV cache — and no single accelerator holds that. The response is **model-parallel sharding**, in two forms:

- **Tensor parallelism** splits the matrices of each layer across GPUs, and is normally kept *within* a node so the splits communicate over the intra-node interconnect (NVLink on NVIDIA hardware) rather than the network.
- **Pipeline parallelism** splits the model's layers into consecutive stages, which is the axis KServe's multi-node mode extends across *multiple nodes*, so a request traverses node after node through the pipeline.

Here the analogy to a sharded cache breaks in the load-bearing place. In a sharded cache one request touches one shard; in model sharding **a single inference touches every shard, in sequence**. The state is partitioned but the computation is not independent, so the interconnect sits on the critical path of every token produced, and every shard must be healthy for any request to complete. Losing one shard does not degrade capacity proportionally — it fails the deployment. This is why multi-node inference is a distinct and harder operating mode than adding replicas, not a larger instance of it.

The two patterns compose rather than compete: a sharded model group is itself the unit that gets replicated once the group's throughput is exhausted.

### Implementation sketch (Scala)

The decision procedure is arithmetic on device memory, and the two topologies differ in how a request fans out. A router that models both:

```scala
final case class Model(name: String, params: Long, bytesPerParam: Int):
  def weightBytes: Long = params * bytesPerParam

final case class Device(id: String, memBytes: Long)

enum Placement:
  case Replicated(perReplica: Device, count: Int)
  case Sharded(stages: Vector[Device])   // every request visits all stages

// The execution surface the router assumes: activations enter, traverse
// devices, and leave as a response.
trait Runtime:
  def pick(replicas: Int): Device
  def encode(r: Request): Activations
  def stage(d: Device)(a: Activations): Activations
  def decode(a: Activations): Response

def place(m: Model, pool: Vector[Device], headroom: Double = 0.9): Option[Placement] =
  val usable = (d: Device) => (d.memBytes * headroom).toLong
  pool.find(d => usable(d) >= m.weightBytes) match
    case Some(d) => Some(Placement.Replicated(d, pool.count(_.memBytes >= d.memBytes)))
    case None    =>
      // No single device holds the weights: accumulate stages until the sum covers them.
      val prefixes = pool.scanLeft((0L, Vector.empty[Device])) { case ((acc, ds), d) =>
        (acc + usable(d), ds :+ d)
      }
      prefixes.find(_._1 >= m.weightBytes).map(p => Placement.Sharded(p._2))

def serve(p: Placement, req: Request)(using rt: Runtime): Response = p match
  case Placement.Replicated(_, n) =>
    rt.decode(rt.stage(rt.pick(n))(rt.encode(req)))                  // one hop
  case Placement.Sharded(stages)  =>
    // Each stage consumes the previous stage's activations, so the transfers
    // between them sit on the critical path.
    rt.decode(stages.foldLeft(rt.encode(req))((act, d) => rt.stage(d)(act)))
```

The shape of `serve` is the argument: the replicated branch is a single dispatch, the sharded branch a fold whose length is the shard count. Adding shards adds latency to every request; adding replicas does not.

## KServe: the patterns as declarative configuration

KServe, a Cloud Native Computing Foundation (CNCF) project whose v0.15 release in June 2025 focused on generative and LLM serving, exposes this hierarchy directly. A single-node replicated deployment is the default; multi-node sharding is opt-in for models that exceed one machine. The replicated case is a short specification:

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: sentiment
spec:
  predictor:
    minReplicas: 0          # scale to zero when idle
    maxReplicas: 8          # replicate for throughput
    scaleMetric: concurrency
    scaleTarget: 70         # in-flight requests per replica, not raw RPS
    model:
      modelFormat: { name: huggingface }
      storageUri: "s3://models/sentiment/"
      resources:
        limits: { nvidia.com/gpu: "1" }
```

For a model requiring sharding across machines, KServe's multi-node mode places one `InferenceService` across a *worker group* of GPU nodes running a tensor- or pipeline-parallel runtime, commonly vLLM. The chosen pattern is therefore a field in the specification rather than a rewrite of the deployment.

No new mental model is required for AI infrastructure. Replication scales throughput, sharding accommodates state that does not fit, and the routing and load-balancing layers are the familiar ones. What changed is that the state is model weights and the commodity is an accelerator, so the penalty for a wrong replicate-versus-shard decision is measured in GPU-hours rather than CPU cycles.

## Pitfalls

- **Sharding a model that fits on one GPU.** Every token then pays an interconnect hop between stages that a single-device placement would not incur; the symptom is higher per-token latency at unchanged throughput.
- **Replicating a model that does not fit.** The pod fails to schedule for lack of device memory, or the runtime spills to host memory and inference slows by orders of magnitude. The parameter count multiplied by bytes per parameter is the check that catches this before deployment.
- **Treating cold start as free under scale-to-zero.** The first request after an idle period absorbs image pull plus weight load — minutes, not milliseconds — and appears as an extreme latency outlier rather than an error.
- **Autoscaling on RPS.** Two workloads at identical request rates but different sequence lengths saturate a GPU at different points, so an RPS threshold either scales up early or leaves the queue growing unnoticed.
- **Assuming shard loss degrades capacity gracefully.** A sharded model group serves no request unless every stage is present, so the failure of one worker node is a total outage of that group, not a proportional loss.
