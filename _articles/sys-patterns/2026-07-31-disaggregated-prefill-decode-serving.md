---
title: "Disaggregated Prefill/Decode: Two GPUs, Two Jobs"
date: 2026-07-31
track: sys-patterns
summary: "Prefill and decode have opposite resource profiles, so co-locating them makes both slower. Splitting them onto separate GPU pools and shipping the KV cache between them is now the default pattern for high-throughput LLM serving."
reading_time: 5
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

An LLM request runs in two phases with almost nothing in common. **Prefill** ingests the whole prompt in one parallel forward pass — it saturates the GPU's math units and is compute-bound. **Decode** then emits one token per step, each step reading the entire KV cache back out of HBM — it starves the math units and is memory-bandwidth-bound. One phase wants FLOPs; the other wants bandwidth.

The classic serving loop runs both on the same GPU and interleaves them in one batch (continuous batching). That is exactly where the trouble starts.

## Why co-location hurts both metrics

You measure the two phases with two different SLOs: **TTFT** (time to first token, set by prefill) and **TPOT/ITL** (time per output token, set by decode). When a chunky prefill lands in a batch that is busy decoding, it monopolizes the compute units and every in-flight decode stalls — TPOT spikes. Throttle prefill to protect decode and your TTFT balloons instead. You are stuck trading one SLO against the other.

Coupling is the deeper problem. On shared GPUs, prefill and decode are forced into the *same* parallelism plan, the same batch, and the same GPU count. But their optimal configs differ: prefill often prefers tensor parallelism to cut TTFT, while decode prefers larger batches to amortize weight reads. One knob can't satisfy both.

DistServe (OSDI '24) named this the **prefill-decode interference** problem and showed the fix: assign the two phases to different GPUs. Doing so let it serve **7.4x more requests** or meet **12.6x tighter SLOs** than co-located baselines while keeping >90% of requests within latency targets. Microsoft's **Splitwise** made the same split across dedicated machine pools and reported **1.4x throughput at 20% lower cost**, or **2.35x throughput** at the same cost and power.

## The pattern

Run two worker pools. Prefill workers process the prompt and produce the KV cache. Decode workers receive that cache and generate tokens. Between them sits a transfer: the KV cache moves from the prefill GPU's VRAM to the decode GPU's VRAM, ideally over NVLink or RDMA rather than through host memory.

```
                 ┌─────────────┐   KV cache    ┌────────────┐
  prompt  ─────▶ │  PREFILL P  │ ============▶ │  DECODE D   │ ──▶ tokens
                 │ compute-bound│  (NIXL/RDMA)  │ bw-bound    │
                 │  TP, big FLOP│               │ big batch   │
                 └─────────────┘               └────────────┘
        scale P and D independently:  xPyD  (e.g. 2P8D)
```

Because the pools are independent, you tune each on its own axis and pick the ratio (`xPyD`) that matches your traffic. Long prompts, short answers? Add prefill workers. The reverse? Add decode workers.

## Doing it in vLLM

vLLM ships this as an experimental feature driven entirely by `--kv-transfer-config`. The two instances run identical models; only the `kv_role` differs. Here it is with the NIXL connector (GPU-to-GPU RDMA):

```bash
# Prefill worker (producer) — GPU 0
CUDA_VISIBLE_DEVICES=0 UCX_NET_DEVICES=all \
VLLM_NIXL_SIDE_CHANNEL_PORT=5600 \
vllm serve Qwen/Qwen3-0.6B --port 8100 \
  --kv-transfer-config \
  '{"kv_connector":"NixlConnector","kv_role":"kv_producer","kv_load_failure_policy":"fail"}'

# Decode worker (consumer) — GPU 1
CUDA_VISIBLE_DEVICES=1 UCX_NET_DEVICES=all \
VLLM_NIXL_SIDE_CHANNEL_PORT=5601 \
vllm serve Qwen/Qwen3-0.6B --port 8200 \
  --kv-transfer-config \
  '{"kv_connector":"NixlConnector","kv_role":"kv_consumer","kv_load_failure_policy":"fail"}'
```

A small proxy sends each request to a prefill worker (`max_tokens=1` to force just the prefill), then hands the KV cache and prompt to a decode worker for the rest. Swap `NixlConnector` for `LMCacheConnector` or `MooncakeStoreConnector` and you get a shared KV store instead of point-to-point transfer.

## Where the ecosystem is (July 2026)

Disaggregation crossed from research into the default production shape this year.

- **NVIDIA Dynamo 1.0** (March 2026) is a cluster orchestrator over runtimes like TensorRT-LLM and SGLang. It runs separate prefill/decode pools, moves KV over **NIXL** (falling back to S3/blob storage off-node), and reconfigures `xPyD` ratios live. Reported adopters include ByteDance, Tencent Cloud, and Together AI.
- **SGLang 0.5.x** and **TensorRT-LLM 1.3** support it natively; SGLang uses `bootstrap_info` for RDMA handshake so prefill keeps serving during transfer.
- **Meta** described its production 1P1D vLLM deployment on H100s over plain TCP, overlapping KV transfer with the forward pass so it adds near-zero latency — a reminder you don't need NVLink to get started.

The tradeoff is real: you now move gigabytes of KV cache per request, so the interconnect is your new bottleneck. Below a certain load, or on a single node, co-location still wins on simplicity. Disaggregation pays off when you're bandwidth-starved on decode, SLO-bound on TTFT, or big enough to run separate pools.

**Try next:** Spin up the two vLLM workers above on any two-GPU box, put a request-splitting proxy in front, and load-test with a fixed 2000-in / 150-out workload. Compare TTFT and ITL against a single co-located instance at the same QPS — then flip from 1P1D to 1P2D and watch which SLO moves.
