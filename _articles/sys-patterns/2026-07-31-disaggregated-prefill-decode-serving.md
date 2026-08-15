---
title: "Disaggregated Prefill/Decode: Two GPU Pools, Two Jobs"
date: 2026-07-31
track: sys-patterns
summary: "Prefill and decode have opposite resource profiles, so co-locating them degrades both. Splitting them onto separate GPU pools and shipping the KV cache between them is now a standard pattern for high-throughput LLM serving."
reading_time: 6
tags: [llm-serving, disaggregation, prefill, decode, kv-cache, ai-infrastructure]
sources:
  - title: "DistServe: Disaggregating Prefill and Decoding for Goodput-optimized LLM Serving (OSDI '24)"
    url: "https://arxiv.org/abs/2401.09670"
  - title: "Splitwise: Efficient generative LLM inference using phase splitting (Microsoft Research)"
    url: "https://www.microsoft.com/en-us/research/blog/splitwise-improves-gpu-usage-by-splitting-llm-inference-phases/"
  - title: "Disaggregated Prefilling — vLLM documentation"
    url: "https://docs.vllm.ai/en/latest/features/disagg_prefill/"
  - title: "Disaggregated Serving — NVIDIA Dynamo documentation"
    url: "https://docs.dynamo.nvidia.com/dynamo/design-docs/disaggregated-serving"
  - title: "Disaggregated Inference at Scale with PyTorch & vLLM"
    url: "https://pytorch.org/blog/disaggregated-inference-at-scale-with-pytorch-vllm/"
---

**Gist.** A large language model (LLM) request runs in two phases with opposite resource profiles, and a serving loop that interleaves them on one graphics processing unit (GPU) forces each phase to degrade the other. Disaggregated serving assigns the phases to separate GPU pools and transfers the key/value (KV) cache between them, so each pool can be sized and parallelised on its own axis. The cost is a per-request transfer of the KV cache across an interconnect, which becomes the new bottleneck and adds a failure mode that co-located serving does not have.

## The two phases

**Prefill** ingests the entire prompt in one parallel forward pass. Every prompt token is processed at once, so the arithmetic units are kept busy and the phase is **compute-bound**. Its output is the KV cache: the per-layer key and value tensors for every prompt token, resident in high-bandwidth memory (HBM).

**Decode** then emits one token per step. Each step performs a small amount of arithmetic but must read the entire KV cache — and the model weights — back out of HBM, so the phase is **memory-bandwidth-bound**. The KV cache grows by one position per step, so the bytes moved per step increase over the life of the request.

One phase is limited by floating-point operations per second (FLOPS); the other by bytes per second. The classic serving loop runs both on the same GPU and mixes them in one batch (continuous batching).

## Why co-location degrades both metrics

The two phases are measured by two different service-level objectives (SLOs): **time to first token (TTFT)**, set by prefill, and **time per output token (TPOT)**, also reported as inter-token latency (ITL), set by decode. When a large prefill enters a batch that is decoding, it occupies the compute units and every in-flight decode step stalls behind it, so **TPOT spikes**. Throttling prefill to protect decode moves the cost to the other side and **TTFT rises**. On a shared GPU there is no setting that satisfies both; the scheduler only chooses which SLO to violate.

The deeper constraint is configuration coupling. Co-located phases share **the same parallelism plan, the same batch, and the same GPU count**. Their preferred configurations differ: prefill benefits from tensor parallelism, which splits a single forward pass across devices to cut TTFT, while decode benefits from larger batches, which amortise the fixed cost of reading model weights across more concurrent sequences. A single knob cannot be set to both values.

DistServe (OSDI '24) named this the **prefill-decode interference** problem and evaluated the separation. It reported serving **7.4x more requests**, or meeting **12.6x tighter SLOs**, than co-located baselines while keeping more than 90% of requests within latency targets. Microsoft's **Splitwise** applied the same split across dedicated machine pools and reported **1.4x throughput at 20% lower cost**, or **2.35x throughput** at equal cost and power.

## The pattern and its invariant

Two worker pools run the same model. Prefill workers consume the prompt and produce the KV cache. Decode workers receive that cache and generate the remaining tokens. Between them is a transfer of the KV cache from the prefill GPU's memory to the decode GPU's memory, over NVLink or remote direct memory access (RDMA) rather than through host memory where the interconnect allows it.

```
                 ┌───────────────┐  KV cache   ┌───────────────┐
  prompt  ─────▶ │   PREFILL P   │ ==========▶ │   DECODE D    │ ──▶ tokens
                 │ compute-bound │ (NIXL/RDMA) │ bandwidth-bd. │
                 │ tensor-par.   │             │ large batch   │
                 └───────────────┘             └───────────────┘
        scale P and D independently:  xPyD  (e.g. 2P8D)
```

The invariant that makes the split correct is that **the decode worker must hold a KV cache byte-identical to the one the prefill worker produced, for the same model and the same token sequence, before its first decode step**. Both pools therefore serve identical models; only the role differs. A request that reaches decode with a missing or partial cache cannot be repaired locally — the prompt would have to be prefilled again.

Because the pools are independent, each is tuned on its own axis and the ratio (`xPyD`) is chosen to match the traffic mix. A workload of long prompts and short answers is prefill-heavy; the inverse is decode-heavy.

## Configuration in vLLM

vLLM ships this as an experimental feature driven by `--kv-transfer-config`. The two instances run identical models and differ in `kv_role`. The example below uses the NIXL connector, which performs GPU-to-GPU RDMA.

```bash
# Prefill worker (producer) — GPU 0
CUDA_VISIBLE_DEVICES=0 UCX_NET_DEVICES=all \
VLLM_NIXL_SIDE_CHANNEL_PORT=5600 \
vllm serve Qwen/Qwen3-0.6B --port 8100 \
  --kv-transfer-config \
  '{"kv_connector":"NixlConnector","kv_role":"kv_producer"}'

# Decode worker (consumer) — GPU 1
CUDA_VISIBLE_DEVICES=1 UCX_NET_DEVICES=all \
VLLM_NIXL_SIDE_CHANNEL_PORT=5601 \
vllm serve Qwen/Qwen3-0.6B --port 8200 \
  --kv-transfer-config \
  '{"kv_connector":"NixlConnector","kv_role":"kv_consumer"}'
```

A proxy in front routes each request first to a prefill worker with `max_tokens=1`, which forces the prefill pass and no more, then hands the KV cache and prompt to a decode worker for the remainder. Substituting `LMCacheConnector` or `MooncakeStoreConnector` for `NixlConnector` replaces the point-to-point transfer with a shared KV store.

### Implementation sketch (Scala)

The routing state machine the proxy implements: one prefill lease, one transfer handshake, one decode lease, and an explicit terminal state when the cache does not arrive.

```scala
type WorkerId = String

enum Stage:
  case Prefilling(worker: WorkerId)
  case Transferring(from: WorkerId, to: WorkerId)
  case Decoding(worker: WorkerId)
  case Failed(reason: String)

final case class Request(id: String, prompt: Seq[Int], stage: Stage)

trait Pool:
  def lease(): Option[WorkerId]
  def release(w: WorkerId): Unit

// transferKv is the connector call (NIXL, LMCache, Mooncake): true once the
// decode worker can read the whole cache.
final class Router(prefill: Pool, decode: Pool, transferKv: (Request, WorkerId, WorkerId) => Boolean):

  // Both slots are leased before prefill begins: if the decode slot were taken
  // only after transfer, the KV cache could arrive with nowhere to land and the
  // prompt would have to be prefilled a second time.
  def admit(r: Request): Request =
    (prefill.lease(), decode.lease()) match
      case (Some(p), Some(d)) => run(r.copy(stage = Stage.Prefilling(p)), p, d)
      case (Some(p), None)    => prefill.release(p); r.copy(stage = Stage.Failed("no decode slot"))
      case (None, Some(d))    => decode.release(d); r.copy(stage = Stage.Failed("no prefill slot"))
      case _                  => r.copy(stage = Stage.Failed("both pools saturated"))

  private def run(r: Request, p: WorkerId, d: WorkerId): Request =
    val prefilled = r.copy(stage = Stage.Transferring(p, d))
    val moved = transferKv(prefilled, p, d)
    prefill.release(p)                      // prefill slot is held until the cache has moved
    if moved then prefilled.copy(stage = Stage.Decoding(d))
    else
      decode.release(d)
      prefilled.copy(stage = Stage.Failed("kv transfer lost"))
```

## Ecosystem, as of July 2026

- **NVIDIA Dynamo** is a cluster orchestrator over runtimes including vLLM, TensorRT-LLM and SGLang. It runs separate prefill and decode pools, moves KV over the **NIXL** transfer library, and treats the prefill-to-decode ratio as a deployment parameter rather than a compile-time one.
- **SGLang** and **TensorRT-LLM** support the split natively as well; the vLLM documentation describes it as an experimental feature rather than a settled interface.
- The **PyTorch and vLLM** teams describe running disaggregated inference in production, with the KV transfer overlapped against compute so that it adds little to end-to-end latency. The published descriptions do not make NVLink a precondition for the pattern.

The trade-off is direct: gigabytes of KV cache now cross an interconnect per request, and that interconnect becomes the limiting resource. Below some load, or on a single node, co-location remains simpler and adequate. Disaggregation pays when decode is bandwidth-starved, when TTFT is SLO-bound, or when the deployment is large enough to run separate pools at all.

## Pitfalls

- **Prefill and decode workers running different model revisions.** The decode worker consumes a KV cache whose layout depends on the model; a mismatch yields corrupted output rather than an error, because the tensors are dimensionally plausible.
- **Leasing the decode slot only after transfer completes.** The cache arrives with no destination and the request either fails or must be prefilled a second time, doubling its compute cost.
- **Treating a lost KV transfer as retriable at the decode worker.** The decode worker holds no prompt state that can regenerate the cache; the retry has to re-enter the prefill pool.
- **Sizing `xPyD` from average traffic.** The ratio is set by the prompt-to-output length mix, and a shift toward long prompts starves the prefill pool while decode workers idle.
- **Routing KV transfers through host memory by default.** The transfer then crosses PCIe twice and the interconnect cost can exceed the interference it was introduced to remove.
- **Benchmarking with TTFT alone.** Disaggregation moves cost between the two SLOs; a TTFT improvement measured without TPOT does not establish that the split helped.
