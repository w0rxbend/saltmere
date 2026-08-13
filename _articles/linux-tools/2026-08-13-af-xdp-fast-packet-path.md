---
title: "AF_XDP: a fast path from the NIC driver straight into your process"
date: 2026-08-13
track: linux-tools
summary: "AF_XDP sockets let an XDP program redirect raw frames from the driver into userspace through shared-memory rings, skipping the kernel network stack entirely. Here's how UMEM, the four rings, and zero-copy mode fit together, plus a minimal libxdp receiver you can actually build."
reading_time: 6
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

The kernel network stack does a lot per packet: allocate an skb, run netfilter, route, demultiplex to a socket, copy to userspace. For a firewall or a browser that's the right trade. For a packet capture engine, a software load balancer, or a DDoS scrubber pushing millions of packets per second, it's overhead you'd rather not pay. **AF_XDP** is the socket family that opts out: an XDP program running in the NIC driver hands raw frames directly to your process through shared-memory rings, before the stack ever sees them.

## How a frame gets redirected

XDP programs run at the earliest possible point — in the driver, on the raw DMA buffer. Normally they return verdicts like `XDP_PASS` or `XDP_DROP`. The third option is `XDP_REDIRECT`, and one redirect target is an **XSKMAP** (`BPF_MAP_TYPE_XSKMAP`): a map whose values are AF_XDP sockets, indexed by queue ID. The canonical program is three lines:

```c
SEC("xdp")
int xsk_redir(struct xdp_md *ctx)
{
    return bpf_redirect_map(&xsks_map, ctx->rx_queue_index, XDP_PASS);
}
```

Frames arriving on queue N go to the socket registered at slot N; anything unmatched falls through to the normal stack. That last part matters — unlike a DPDK port takeover, the interface keeps working. ARP, SSH, and your monitoring keep flowing through the kernel while your fast path grabs only the queue (or traffic) you steer to it. In practice you don't even write this program: **libxdp** loads an equivalent default program and manages the map for you when you create a socket.

## UMEM and the four rings

An AF_XDP socket owns no packet buffers. You allocate one contiguous region called the **UMEM**, divided into equal-sized frames (typically 4096 bytes), and register it with the kernel. All packet data lives there, in your address space; the rings only carry 64-bit descriptors pointing into it.

| Ring | Producer | Consumer | Carries |
|------|----------|----------|---------|
| Fill | you | kernel | empty frames for RX |
| RX | kernel | you | received frames (addr + len) |
| TX | you | kernel | frames to transmit |
| Completion | kernel | you | sent frames, ready to reuse |

The lifecycle is a loop: you push free frame addresses onto the **fill ring**; the driver writes packets into them and posts descriptors on the **RX ring**; you process and recycle the addresses back to the fill ring. Transmit mirrors it through the TX and **completion** rings. Everything is single-producer/single-consumer shared memory — after setup, a busy-polling receiver does zero syscalls per packet.

## Copy, zero-copy, and need_wakeup

Bind flags select the data path. `XDP_COPY` works everywhere: the driver still copies each frame into your UMEM, which already skips skb allocation and the stack. `XDP_ZEROCOPY` is the real prize — the driver DMAs packets *directly into your UMEM frames*, so the NIC writes and your process reads the same bytes. Zero-copy needs driver support (i40e, ice, mlx5, and friends; virtio-net gained it more recently) and only works in native driver mode, not the generic `xdpgeneric` fallback. The usual approach: request `XDP_ZEROCOPY`, fall back to copy mode if `bind()` returns `EOPNOTSUPP`.

Add `XDP_USE_NEED_WAKEUP` and the kernel sets a flag on the ring when it actually needs a `poll()`/`sendto()` kick, letting you spare the syscall on the hot path but sleep when idle — the pragmatic middle ground between busy-polling a core at 100% and paying wakeup latency.

## A minimal receiver with libxdp

The `xsk_*` API used to live in libbpf and moved to libxdp, part of [xdp-tools](https://github.com/xdp-project/xdp-tools) (v1.6.3 as of mid-2026). On Debian/Ubuntu: `apt install libxdp-dev xdp-tools`. The essential shape, error handling elided:

```c
#include <xdp/xsk.h>
#define FRAMES 4096
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

for (;;) {                                /* the actual fast path */
    uint32_t ridx, n = xsk_ring_cons__peek(&rx, 64, &ridx);
    for (uint32_t i = 0; i < n; i++) {
        const struct xdp_desc *d = xsk_ring_cons__rx_desc(&rx, ridx + i);
        handle(xsk_umem__get_data(bufs, d->addr), d->len);
        /* then recycle d->addr back onto fq */
    }
    xsk_ring_cons__release(&rx, n);
}
```

Build with `gcc rx.c -o rx $(pkg-config --cflags --libs libxdp)`, run as root, and confirm the program attached with `xdp-loader status eth0`. To see *all* traffic instead of one queue's, either steer flows to that queue with `ethtool -N` or open one socket per queue. `xdpdump` from the same package is a ready-made sanity check that the redirect path works on your NIC.

## When it's the right tool

Against **plain sockets** (even `AF_PACKET` with `PACKET_MMAP`), AF_XDP wins on per-packet cost: no skb, no stack traversal, optionally no copy and no syscalls. Against **DPDK**, it trades a little peak throughput for a lot of operability: the kernel still owns the device, standard tooling keeps working, no hugepage/VFIO setup, no vendor PMD matrix — just a driver with XDP support. DPDK still holds the crown when you need every last nanosecond and exotic NIC offloads; AF_XDP is the answer when you want 80–90% of that inside a normal Linux process. If your rates are tens of thousands of packets per second, not millions, stay with plain sockets — the complexity isn't free.

**Try next:** run `xdpdump -i eth0 --rx-capture entry` while pinging the box, then modify the receiver above to swap Ethernet src/dst MACs and bounce frames back out through the TX ring — a userspace reflector is the "hello world" of packet processing.
