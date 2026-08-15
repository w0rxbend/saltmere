---
title: "io_uring zero-copy receive (zcrx): NIC-to-userspace without the copy"
date: 2026-08-15
track: linux-tools
summary: "At 100–200 Gbit/s the bottleneck of receiving network data is no longer the syscall — it is the memcpy from kernel socket buffers into the application's buffer. io_uring zcrx, merged in Linux 6.15, has the network interface controller place packet payloads directly into user-registered memory while headers stay in the kernel. This article covers the mechanism — header splitting, net_iov memory providers, refill rings — the hardware it requires, and the workload shape it serves."
reading_time: 6
tags: [io_uring, zero-copy, networking, kernel, liburing, nic]
sources:
  - title: "io_uring zero copy Rx — kernel networking documentation"
    url: "https://docs.kernel.org/networking/iou-zcrx.html"
  - title: "io_uring zero copy rx (LWN.net)"
    url: "https://lwn.net/Articles/994603/"
  - title: "io_uring zcrx ifq sharing (LWN.net)"
    url: "https://lwn.net/Articles/1043867/"
  - title: "IO_uring Network Zero-Copy Receive Lands In Linux 6.15 (Phoronix)"
    url: "https://www.phoronix.com/news/Linux-6.15-IO_uring"
  - title: "io_uring zerocopy send (LWN.net)"
    url: "https://lwn.net/Articles/877167/"
---

**Gist.** Even a perfectly batched `recv` ends with the kernel copying the payload from a socket buffer into application memory, and at 100–200 Gbit/s that copy dominates both CPU time and memory bandwidth. **zcrx** — io_uring zero-copy receive, developed by David Wei (Meta) and Pavel Begunkov and **merged in Linux 6.15** (May 2025) — removes the copy by having the network interface controller (NIC) write payloads directly into memory the process registered in advance, leaving only headers to the kernel. The cost is structural: the mechanism binds to a specific hardware receive queue on a NIC that supports header/data splitting and flow steering, so it is provisioned per flow rather than per connection.

The [earlier io_uring article](/articles/linux-tools/2026-07-26-io-uring-async-syscalls) covered what the two rings buy: batched submission and completion without syscalls. zcrx addresses the residual per-byte cost that ring batching leaves untouched — the payload is touched twice, once by the direct memory access (DMA) write into the kernel's receive buffers and once by the copy out of them.

## Why receive is harder than send

Zero-copy *send* landed in **Linux 6.0** as `IORING_OP_SEND_ZC`. The kernel pins the caller's buffer, points the NIC at it, and posts a **second completion** once the hardware has finished with the memory, marking the buffer reusable. Send is tractable because the data's address is known before the transfer starts.

Receive inverts the ordering. A packet arrives before anything in software knows which socket it belongs to, so placing its payload in the correct process's memory is a decision that must be made **in hardware, before the kernel inspects a byte**. That constraint is what produces zcrx's hardware requirements rather than a purely software fast path.

## The mechanism

Three NIC capabilities carry the design:

- **Header/data splitting** — the NIC splits each packet at the layer-4 boundary. Headers land in ordinary kernel memory, so the **TCP stack runs unmodified**: acknowledgements, reordering and retransmission are unaffected. The **payload** goes to a separate set of buffers.
- **Flow steering** — an `ethtool -N` rule binds one flow (identified by protocol and destination port) to one dedicated hardware receive queue.
- **Receive-side scaling (RSS)** — configured to keep all *other* traffic off that queue, so the queue's buffers serve only the registered flow.

The process registers three objects against that queue through `IORING_REGISTER_ZCRX_IFQ`, which liburing wraps as `io_uring_register_ifq()`: an **interface queue (ifq)**, a memory-mapped **area** for payloads, and a **refill ring**. The area's pages become **net_iov** buffers backing a page-pool *memory provider* attached to that receive queue. The invariant that makes the copy disappear is that **the driver's page-pool allocations for this queue are satisfied from the registered area**, so the NIC's DMA target is already the application's memory.

Reception is a multishot `IORING_OP_RECV_ZC` request. Each completion carries an **offset and length into the registered area** rather than copied bytes. The ring must be created with 32-byte completion queue entries (`IORING_SETUP_CQE32`) plus `IORING_SETUP_SINGLE_ISSUER` and `IORING_SETUP_DEFER_TASKRUN`.

The buffer lifecycle is a two-phase loop with no kernel-side reclaim: a net_iov is either **held by the application** (referenced by an outstanding completion) or **available to the page pool**. Returning a chunk means pushing its descriptor onto the **refill ring**; the page pool then hands it back to the NIC. The failure mode follows directly — **buffers held too long are not available for receive, and the queue starves itself**. The symptom is receive stalling on a link that is otherwise idle, and the cause is application-side retention, not congestion.

| | classic `recv` | `SEND_ZC` (6.0) | zcrx (6.15) |
|---|---|---|---|
| Payload copies | 1 (kernel→user) | 0 | 0 |
| Special NIC features | none | none | header split + flow steering |
| Setup cost | none | pin buffers | queue + ifq + area registration |
| Granularity | any socket | any socket | per hardware RX queue |

## Requirements and ongoing work

Driver support is the gating factor. zcrx landed with a small set of supporting drivers, **Broadcom's bnxt_en** among them, and the driver's own documentation is the authority on whether a given device qualifies. Development has continued: an **ifq sharing** series proposes letting multiple rings share a single hardware receive queue, relaxing the one-ring-per-queue coupling of the initial merge.

The device-side setup, condensed from the kernel networking documentation:

```bash
# split headers from payloads, carve out queue 7, steer the flow to it
ethtool -G eth0 tcp-data-split on
ethtool -X eth0 equal 7          # RSS: spread other traffic over queues 0-6
ethtool -N eth0 flow-type tcp6 dst-port 9999 action 7
```

Each line is load-bearing. Without `tcp-data-split on` the NIC has no separate payload buffers to place into the registered area. Without the `-X` restriction, unrelated traffic lands on queue 7 and consumes net_iov buffers that the registered flow needs. Without the `-N` rule, the registered flow is hashed onto some other queue and never reaches the zcrx path at all.

The reference implementation is the kernel selftest **`tools/testing/selftests/drivers/net/hw/iou-zcrx.c`** in the kernel tree, which exercises the full register–receive–refill loop against liburing.

## Applicability

zcrx targets **few, high-volume flows** where per-byte cost dominates: machine-learning training clusters ingesting datasets, storage backends replicating at line rate, video origin servers. The benefit scales with bytes per flow, so a service handling many small requests per second gains nothing — and because steering is per hardware receive queue, dedicated queues are provisioned **per hot flow, not per connection**, which bounds how many flows a single NIC can serve this way. The requirements are Linux 6.15 or newer together with a supporting driver. Where the shape fits, the effect is narrow and blunt: the largest CPU consumer in a high-bandwidth receiver's profile — the payload copy — is removed rather than reduced.

A direct measurement: on a 6.15-or-newer machine with a NIC whose driver supports zcrx, building `iou-zcrx.c` from the kernel selftests, steering an `iperf3` flow at it, and comparing `perf top` against a plain `recv` receiver shows whether `copy_user_generic` still appears in the profile.

## Pitfalls

- **Payload buffers retained across many completions stall reception.** The page pool cannot refill the receive queue while the application holds the net_iovs; the link appears healthy while receive makes no progress.
- **Omitting `IORING_SETUP_CQE32` breaks the completion format.** zcrx completions carry an area offset and length in the extended entry; a 16-byte completion queue entry has nowhere to put them.
- **Traffic other than the registered flow reaching the steered queue consumes the registered area.** Without the RSS restriction the queue is a general-purpose queue that happens to be backed by application memory.
- **A misdirected `ethtool -N` rule fails silently at the application layer.** The connection works normally over the ordinary receive path with the copy intact, so the absence of the optimisation is visible only in a CPU profile.
- **The registration is bound to one hardware receive queue, not to a socket.** Scaling the connection count does not scale the mechanism; as merged, one ring maps to one queue.
- **Driver support, not kernel version, is the real gate.** Running 6.15 or newer on a NIC whose driver lacks a page-pool memory provider yields a failed registration, not a slower fast path.
