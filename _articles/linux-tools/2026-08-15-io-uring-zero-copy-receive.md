---
title: "io_uring zero-copy receive (zcrx): NIC-to-userspace without the copy"
date: 2026-08-15
track: linux-tools
summary: "At 100–200 Gbit/s the bottleneck of receiving network data is no longer syscalls — it's the memcpy from kernel socket buffers into your buffer. io_uring zcrx, merged in Linux 6.15, has the NIC DMA packet payloads straight into user-registered memory while headers stay in the kernel. Here's the mechanism — header splitting, net_iov memory providers, refill rings — what hardware it needs, and who actually benefits."
reading_time: 5
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

The [earlier io_uring article](/articles/linux-tools/io-uring-async-syscalls) covered what the two rings buy you: batched submission, completion without syscalls. But even a perfectly batched `recv` still ends the same way — the NIC DMAs the packet into a kernel `sk_buff`, and the kernel **memcpy**s the payload into your buffer. At 1 Gbit/s nobody notices. At 100 or 200 Gbit/s, that copy *is* the workload: it burns CPU cycles and, worse, memory bandwidth, twice touching every byte you receive. **zcrx** — io_uring zero-copy receive, developed by David Wei (Meta) and Pavel Begunkov and **merged in Linux 6.15** (May 2025) — deletes the copy. The NIC writes packet payloads directly into memory your process registered; the kernel only ever touches the headers.

## Send was the easy half

Zero-copy *send* landed back in **Linux 6.0** as `IORING_OP_SEND_ZC`: the kernel pins your buffer and points the NIC at it, then posts a second completion when the hardware is done so you know when the buffer is reusable. Send is tractable because you choose the data's address before anything happens. Receive is the hard direction — a packet arrives before anyone knows which socket it belongs to, so "DMA it to the right process's memory" requires deciding *in hardware*, before the kernel sees a byte. That's why zcrx took three more years and why it has real hardware requirements.

## How zcrx works

Three NIC features do the heavy lifting:

- **Header/data splitting** — the NIC splits each packet at the L4 boundary: headers go into normal kernel memory (the TCP stack still runs, unmodified — ACKs, reordering, retransmits all work), while the **payload** goes into separate buffers.
- **Flow steering** — an `ethtool -N` rule pins your flow (say, TCP port 9999) to one dedicated hardware RX queue.
- **RSS** — configured to keep all *other* traffic off that queue.

Your process then registers three things against that queue via `IORING_REGISTER_ZCRX_IFQ` (liburing wraps this as `io_uring_register_ifq()`): an **interface queue (ifq)** object, a big mmap'd **memory area** for payloads, and a **refill ring**. The registered area's pages become **net_iov** buffers backing a special page-pool *memory provider* for that RX queue — so when the driver asks the page pool for RX buffers, it gets your pages, and the NIC DMAs payloads straight into them.

Receiving is a multishot `IORING_OP_RECV_ZC` request. Each completion (the ring must use 32-byte CQEs — `IORING_SETUP_CQE32` — plus `IORING_SETUP_SINGLE_ISSUER` and `IORING_SETUP_DEFER_TASKRUN`) carries an *offset and length into your registered area* instead of copied bytes. When you're done with a chunk, you push it onto the **refill ring** and the page pool hands it back to the NIC. Buffers you sit on too long are simply unavailable for RX — recycle promptly or starve your own queue.

| | classic `recv` | `SEND_ZC` (6.0) | zcrx (6.15) |
|---|---|---|---|
| Payload copies | 1 (kernel→user) | 0 | 0 |
| Special NIC features | none | none | header split + flow steering |
| Setup cost | none | pin buffers | queue + ifq + area registration |
| Granularity | any socket | any socket | per hardware RX queue |

## What you need, and what's still moving

Driver support is the gating factor: it launched with **Broadcom bnxt_en** and **Google gve**, with more drivers (including Mellanox/NVIDIA) arriving in subsequent releases — check your driver's docs before planning around it. Development hasn't stopped either: an **ifq sharing** series (late 2025) lets multiple rings in one process share a single hardware RX queue, addressing the awkward one-ring-per-queue coupling of the initial merge.

The setup dance, condensed from the kernel docs:

```bash
# split headers from payloads, carve out queue 7, steer the flow to it
ethtool -G eth0 tcp-data-split on
ethtool -X eth0 equal 7          # RSS: spread other traffic over queues 0-6
ethtool -N eth0 flow-type tcp6 dst-port 9999 action 7
```

For working code, the kernel selftest is the reference implementation: **`tools/testing/selftests/drivers/net/hw/iou-zcrx.c`** in the kernel tree exercises the full register-receive-refill loop with liburing and is far more instructive than the API docs alone.

## Who should care

zcrx targets a specific shape of workload: **few, fat flows** where per-byte cost dominates — ML training clusters ingesting datasets, storage backends replicating at line rate, video origin servers. Meta built it for exactly that. If your service handles ten thousand small HTTP requests per second, zcrx does nothing for you: the win scales with bytes per flow, and the flow-steering requirement means you provision dedicated queues per hot flow, not per connection. It's also Linux-6.15-or-newer plus cooperative hardware — a real deployment constraint in 2026, though one that ages well. But where it fits, the effect is blunt: the single largest CPU consumer of a high-bandwidth receiver simply disappears from the profile.

**Try next:** on a 6.15+ machine with a bnxt/gve NIC, build `iou-zcrx.c` from the kernel selftests, steer an `iperf3` flow at it, and compare `perf top` against a plain `recv` receiver — watch `copy_user_generic` vanish.
