---
title: "AF_XDP: a fast path from the NIC driver into a userspace process"
date: 2026-08-13
track: linux-tools
summary: "AF_XDP sockets let an XDP program redirect raw frames from the driver into userspace through shared-memory rings, skipping the kernel network stack. How UMEM, the four rings, and zero-copy mode fit together, with a minimal libxdp receiver."
reading_time: 7
tags: [af-xdp, xdp, networking, libxdp, zero-copy]
sources:
  - title: "AF_XDP — Linux kernel networking documentation"
    url: "https://www.kernel.org/doc/html/latest/networking/af_xdp.html"
  - title: "xdp-project/xdp-tools — libxdp and utilities"
    url: "https://github.com/xdp-project/xdp-tools"
  - title: "libxdp README — xdp-tools"
    url: "https://github.com/xdp-project/xdp-tools/blob/master/lib/libxdp/README.org"
  - title: "Accelerating networking with AF_XDP — LWN"
    url: "https://lwn.net/Articles/750845/"
---

**Gist.** The kernel network stack performs a fixed amount of work per packet — socket buffer (skb) allocation, netfilter traversal, routing, demultiplexing to a socket, and a copy to userspace — which becomes the dominant cost for applications processing millions of packets per second. **AF_XDP** is a socket address family that bypasses that path: an eXpress Data Path (XDP) program running inside the network interface controller (NIC) driver redirects raw frames into a userspace-owned shared-memory region before the stack is entered. The cost is that the application takes over buffer management, descriptor bookkeeping and per-queue steering, and must handle the case where the driver does not support zero-copy.

## How a frame gets redirected

XDP programs run at the earliest point at which the driver has a packet available — on the raw direct-memory-access (DMA) buffer, before an skb exists. The usual verdicts are `XDP_PASS` and `XDP_DROP`. The verdict that feeds AF_XDP is `XDP_REDIRECT`, whose target may be an **XSKMAP** (`BPF_MAP_TYPE_XSKMAP`): a map whose values are AF_XDP sockets, indexed by queue identifier. The canonical program is a single redirect:

```c
SEC("xdp")
int xsk_redir(struct xdp_md *ctx)
{
    return bpf_redirect_map(&xsks_map, ctx->rx_queue_index, XDP_PASS);
}
```

The invariant is per-queue: **a frame arriving on receive queue N is delivered to the socket registered at XSKMAP slot N**, and the third argument supplies the fallback verdict when no socket occupies that slot. Here the fallback is `XDP_PASS`, so unmatched traffic continues into the ordinary stack. That fallback is what separates AF_XDP from a Data Plane Development Kit (DPDK) port takeover: **the kernel retains ownership of the interface**, so Address Resolution Protocol (ARP), administrative SSH sessions and monitoring traffic continue to work while the fast path receives only the queues, or the flows, that are steered to it. **libxdp** loads an equivalent default program and maintains the map when a socket is created, so the program above need not be written by hand.

## UMEM and the four rings

An AF_XDP socket owns no packet buffers of its own. The application allocates one contiguous region, the **user memory area (UMEM)**, divides it into equal-sized frames (commonly 4096 bytes) and registers it with the kernel. **All packet data resides in that region, in the application's address space; the rings carry only descriptors — a 64-bit offset into the UMEM, and for the RX and TX rings a length alongside it — never payload.**

| Ring | Producer | Consumer | Carries |
|------|----------|----------|---------|
| Fill | application | kernel | empty frames for RX |
| RX | kernel | application | received frames (addr + len) |
| TX | application | kernel | frames to transmit |
| Completion | kernel | application | sent frames, ready to reuse |

The receive cycle is a closed loop over frame ownership. The application publishes free frame addresses on the **fill ring**; the driver writes packet data into those frames and posts descriptors on the **RX ring**; the application consumes the descriptors, processes the data in place, and returns the addresses to the fill ring. Transmit mirrors this through the TX and **completion** rings: a descriptor placed on the TX ring transfers the frame to the kernel, and its address reappears on the completion ring once the frame has been sent.

Each ring is **single-producer/single-consumer shared memory**, which is what removes the syscall from the steady state: after setup, a receiver that busy-polls the RX ring performs zero syscalls per packet. It is also what makes the address-recycling discipline load-bearing. **A frame address is owned by exactly one side at a time.** Losing an address — consuming an RX descriptor without ever returning its address to the fill ring — permanently removes that frame from circulation; the leak is silent and ends in an empty fill ring, at which point the driver has nowhere to place arriving packets and drops them.

## Copy, zero-copy, and need_wakeup

Bind flags select the data path. `XDP_COPY` works in both native and generic mode: the driver copies each frame into the UMEM, which still eliminates skb allocation and the stack traversal but retains one copy. `XDP_ZEROCOPY` instructs the driver to DMA packets **directly into the UMEM frames**, so the NIC writes and the process reads the same bytes. Zero-copy requires explicit driver support (i40e, ice and mlx5 among them; virtio-net gained it more recently) and native driver mode — the generic `xdpgeneric` fallback, which runs XDP after the driver has already built an skb, cannot provide it.

The resulting failure mode is a bind-time one rather than a silent performance regression: **`bind()` with `XDP_ZEROCOPY` returns `EOPNOTSUPP` when the driver or mode cannot satisfy it.** The standard structure is therefore to request zero-copy first and retry with `XDP_COPY` on that error.

`XDP_USE_NEED_WAKEUP` addresses the opposite problem. Without it, an application must choose between busy-polling a core continuously and paying an unconditional `poll()` or `sendto()` syscall per batch. With the flag set, **the kernel raises a flag on the ring when it requires a wakeup**, so the syscall is issued only when the ring's state demands it and the process can sleep when the link is idle.

## A minimal receiver with libxdp

The `xsk_*` API originally lived in libbpf and now lives in libxdp, part of [xdp-tools](https://github.com/xdp-project/xdp-tools). On Debian and Ubuntu the packages are `libxdp-dev` and `xdp-tools`. The essential shape, with error handling elided:

```c
#include <xdp/xsk.h>
#define FRAMES XSK_RING_PROD__DEFAULT_NUM_DESCS  /* fill ring holds no more */
#define FRAME_SIZE XSK_UMEM__DEFAULT_FRAME_SIZE

void *bufs; struct xsk_umem *umem;
struct xsk_ring_prod fq, tx; struct xsk_ring_cons cq, rx;
struct xsk_socket *xsk;

posix_memalign(&bufs, getpagesize(), FRAMES * FRAME_SIZE);
xsk_umem__create(&umem, bufs, FRAMES * FRAME_SIZE, &fq, &cq, NULL);

struct xsk_socket_config cfg = {
    .rx_size = XSK_RING_CONS__DEFAULT_NUM_DESCS,
    .tx_size = XSK_RING_PROD__DEFAULT_NUM_DESCS,
    .bind_flags = XDP_USE_NEED_WAKEUP,   /* libxdp loads the XDP prog */
};
xsk_socket__create(&xsk, "eth0", /*queue*/ 0, umem, &rx, &tx, &cfg);

uint32_t idx;                             /* hand all frames to the kernel */
xsk_ring_prod__reserve(&fq, FRAMES, &idx);
for (int i = 0; i < FRAMES; i++)
    *xsk_ring_prod__fill_addr(&fq, idx++) = i * FRAME_SIZE;
xsk_ring_prod__submit(&fq, FRAMES);

for (;;) {                                /* the fast path */
    uint32_t ridx, n = xsk_ring_cons__peek(&rx, 64, &ridx);
    for (uint32_t i = 0; i < n; i++) {
        const struct xdp_desc *d = xsk_ring_cons__rx_desc(&rx, ridx + i);
        handle(xsk_umem__get_data(bufs, d->addr), d->len);
        /* then recycle d->addr back onto fq */
    }
    xsk_ring_cons__release(&rx, n);
}
```

Three details in that listing carry the mechanism. The initial loop seeds the fill ring with **every** frame address in the UMEM, because a driver with an empty fill ring has no buffer into which to receive; the UMEM is sized to the fill ring's capacity so that the single reservation fits. The `reserve`/`submit` and `peek`/`release` pairs are the ownership transfer: **the producer index advances only at `submit`, and the consumer index only at `release`**, so a descriptor written but not submitted is invisible to the other side. And `xsk_umem__get_data` performs address arithmetic against the UMEM base — the payload was never copied out of the region.

Build with `gcc rx.c -o rx $(pkg-config --cflags --libs libxdp)`, run as root, and confirm attachment with `xdp-loader status eth0`. Because delivery is per queue, a socket bound to queue 0 observes only the traffic the NIC's receive-side steering places on queue 0; capturing all traffic requires steering flows to that queue with `ethtool -N` or opening one socket per queue. `xdpdump` from the same package verifies that the redirect path functions on a given NIC.

## When it is the right tool

Against plain sockets, including `AF_PACKET` with `PACKET_MMAP`, AF_XDP removes the skb allocation and the stack traversal, and optionally the copy and the per-packet syscall. Against DPDK, it trades peak throughput for operability: the kernel continues to own the device, standard tooling continues to work, and there is no hugepage or Virtual Function I/O (VFIO) setup and no vendor poll-mode-driver matrix to satisfy — the requirement is a driver with XDP support. DPDK remains preferable where every nanosecond and exotic NIC offloads matter. Where traffic is measured in tens of thousands of packets per second rather than millions, plain sockets remain the appropriate choice, since the buffer-management complexity described above is paid regardless of rate.

## Pitfalls

- **The fill ring empties and receive stops.** An RX descriptor was consumed without its frame address being returned to the fill ring; the frame leaks out of circulation and the driver eventually has no buffer to write into, so packets are dropped in the driver.
- **`bind()` fails with `EOPNOTSUPP`.** `XDP_ZEROCOPY` was requested on a driver without zero-copy support, or in generic mode, where XDP runs after skb allocation. The socket must be retried with `XDP_COPY`.
- **Traffic is visible to `tcpdump` but never reaches the socket.** The frames arrive on a receive queue whose XSKMAP slot holds no socket, so the redirect falls through to the fallback verdict; only the bound queue is captured.
- **Descriptors are written but never observed by the peer.** `xsk_ring_prod__submit` or `xsk_ring_cons__release` was omitted, leaving the shared producer or consumer index unchanged; the ring appears empty to the other side.
- **Throughput is far below expectation with no errors reported.** The attachment silently landed in `xdpgeneric` mode, where XDP executes after the skb has been built, so the skb allocation the design avoids is still paid; `xdp-loader status` reports the active mode.
- **A core is pinned at 100% while the link is idle.** The socket was bound without `XDP_USE_NEED_WAKEUP`, leaving busy-polling as the only way to avoid an unconditional syscall per batch.
