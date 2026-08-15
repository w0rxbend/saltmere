---
title: "Taming Bufferbloat with the CAKE qdisc on Linux"
date: 2026-07-30
track: linux-tools
summary: "How oversized buffers wreck latency under load, why pfifo_fast cannot help, and how the CAKE qdisc combines a shaper, fq_codel-style AQM, and flow isolation into one tc invocation."
reading_time: 7
tags: [networking, bufferbloat, qos, tc, cake]
sources:
  - title: "tc-cake(8) man page"
    url: "https://man7.org/linux/man-pages/man8/tc-cake.8.html"
  - title: "CAKE — Bufferbloat.net"
    url: "https://www.bufferbloat.net/projects/codel/wiki/Cake/"
  - title: "Piece of CAKE: A Comprehensive Queue Management Solution for Home Gateways (Høiland-Jørgensen, Täht, Morton, 2018)"
    url: "https://arxiv.org/abs/1804.07617"
  - title: "Add Common Applications Kept Enhanced (cake) qdisc [LWN.net]"
    url: "https://lwn.net/Articles/752777/"
  - title: "Linux 4.19 — Kernel Newbies"
    url: "https://kernelnewbies.org/Linux_4.19"
---

**Gist.** When a bulk transfer fills an oversized buffer at the network bottleneck, every other packet queues behind it, and interactive latency collapses even though throughput looks healthy. The CAKE (Common Applications Kept Enhanced) queueing discipline (qdisc) fixes this by **shaping traffic slightly below the true bottleneck rate**, so the managed queue sits on the local host rather than in unmanageable downstream equipment, and by draining that queue with fair-queueing plus active queue management (AQM). The cost is a deliberate sacrifice of headline throughput: the shaper is configured below the real link rate, and every packet passes through per-flow hashing and dwell-time accounting.

## The mechanism of bufferbloat

Between a host and the wider internet there is one bottleneck link — usually the slowest hop, often the residential uplink. When packets arrive faster than that link drains them, they queue. Router and modem vendors have provisioned those queues generously, sometimes hundreds of milliseconds' worth of packets at the link rate. A single bulk Transmission Control Protocol (TCP) flow expands its congestion window until it fills that buffer, because a first-in-first-out (FIFO) queue signals congestion only by overflowing.

The consequence is structural, not a fault of any one flow. **Queueing delay equals queue occupancy divided by drain rate**, so a buffer holding 300 ms of data at the link rate adds 300 ms to every packet behind it, regardless of that packet's size or importance. A speed test still reports the full rate. The diagnostic quantity is instead the difference between **idle round-trip time and round-trip time while the link is saturated**. On a bloated link that difference is the standing queue's drain time, which is why the increase is measured in tens or hundreds of milliseconds rather than in percent.

## Why a FIFO qdisc cannot correct it

The default qdisc on many Linux interfaces is `pfifo_fast`: a FIFO queue with three priority bands. Its contract is to hold packets until the link is free; it makes no statement about **how many** it holds. Given a large buffer, it keeps that buffer full.

Two properties bound what priority bands can achieve. First, they reorder only traffic already resident in that qdisc, so a buffer sitting downstream in a modem is outside their reach entirely. Second, the only congestion signal a FIFO emits is a tail drop at overflow, which occurs after the queue is already at maximum depth — the latency damage is complete before the signal is sent, and it arrives to many flows simultaneously.

The approach developed by the bufferbloat.net and CeroWrt work (Dave Täht, Toke Høiland-Jørgensen, Jonathan Morton, and collaborators) addresses both properties at once: **relocate the queue to a place under local control by shaping to a rate under the real bottleneck rate**, and **manage that queue actively** so congestion is signalled before the queue is full.

## What CAKE combines

CAKE is the successor to the Smart Queue Management (SQM) scripts that composed Hierarchical Token Bucket (`htb`) with `fq_codel`, folded into a single qdisc. It merged into mainline Linux in **4.19** (October 2018); before that it existed as an out-of-tree module and shipped in OpenWrt/LEDE. Four components:

- **A deterministic shaper.** CAKE rate-limits traffic itself, so the downstream buffer never fills. It accounts for link-layer overhead (Asynchronous Transfer Mode / Packet Transfer Mode framing on digital subscriber lines, Ethernet framing) so that the shaped rate corresponds to bytes transmitted on the wire rather than to payload bytes.
- **fq_codel-style AQM.** Controlled Delay (CoDel) measures **sojourn time** — how long a packet dwelt in the queue — and begins marking or dropping when sojourn time remains above target, signalling TCP to reduce its window before the buffer fills. CAKE marks with Explicit Congestion Notification (ECN) where the endpoints negotiate it, avoiding the retransmission a drop would cost.
- **Flow isolation.** The default `triple-isolate` mode uses an **8-way set-associative hash**, so each flow receives its own effective queue and a bulk transfer cannot starve a latency-sensitive one. With the `nat` keyword, CAKE resolves the pre-translation address, isolating by real host behind Network Address Translation rather than by the translated tuple.
- **Optional Differentiated Services "tins."** Traffic can be sorted into priority classes by Differentiated Services Code Point (DSCP) marks (`diffserv3`, `diffserv4`, `diffserv8`, or `besteffort` to ignore marks). **Tins carry bandwidth guarantees rather than strict priority**, bounding how much of the link a high-priority class can take.

The "Piece of CAKE" paper (Høiland-Jørgensen, Täht, Morton, 2018) argues that an improved queue algorithm alone is insufficient when the oversized buffer resides in legacy equipment that cannot be replaced — hence the integrated shaper. The invariant the configuration must preserve is that **the CAKE queue is the slowest point on the path**; if the shaper rate exceeds the true bottleneck rate, the downstream FIFO becomes the bottleneck again and the AQM sees an empty queue it can do nothing with.

## Applying it with tc

Egress (upload) direction, minimal form:

```sh
# Set bandwidth ~5-10% BELOW the measured upload rate
sudo tc qdisc add dev eth0 root cake bandwidth 100mbit
```

A fuller egress configuration — path round-trip time, NAT-aware host fairness, and link-layer accounting:

```sh
sudo tc qdisc replace dev eth0 root cake \
    bandwidth 100mbit rtt 100ms nat ethernet
```

Per-tin counters and drop/mark statistics:

```sh
tc -s qdisc show dev eth0
```

Controlling the **download** direction requires shaping ingress, which means redirecting incoming traffic through an Intermediate Functional Block (IFB) device. The buffer in question belongs to the internet service provider, so the only available lever is pacing what is pulled through it:

```sh
sudo modprobe ifb
sudo ip link add ifb0 type ifb 2>/dev/null || true
sudo ip link set ifb0 up
sudo tc qdisc add dev eth0 handle ffff: ingress
sudo tc filter add dev eth0 parent ffff: protocol all u32 \
    match u32 0 0 action mirred egress redirect dev ifb0
sudo tc qdisc add dev ifb0 root cake bandwidth 40mbit besteffort
```

Removal after testing:

```sh
sudo tc qdisc del dev eth0 root
```

| Option | Effect |
| --- | --- |
| `bandwidth RATE` | Shaper rate — set below the true bottleneck |
| `rtt TIME` | Tunes CoDel's target; `internet` is the default |
| `nat` | Isolate by real host behind NAT, not the NAT'd tuple |
| `diffserv4` / `besteffort` | Honour DSCP tins, or ignore marks entirely |
| `ack-filter` | Thin redundant TCP ACKs on asymmetric links |
| `ethernet` / `atm` / `docsis` | Link-layer overhead compensation keyword |

## Measurement procedure

The effect is measurable directly: saturate the link and probe latency through it.

```sh
iperf3 -c <server> -t 60        # add -R to test the download direction
```

Concurrently:

```sh
ping -i 0.2 1.1.1.1
```

Run first with the default qdisc and record the gap between idle and loaded round-trip time; on a bloated path the loaded figure rises by roughly the buffer's drain time. Apply CAKE with an appropriate `bandwidth` and repeat; the gap should shrink to the sojourn time CoDel is allowed to hold.

For a repeatable comparison, **flent** (the FLExible Network Tester, from the same community) provides the `rrul` test, which loads the link in both directions while plotting latency:

```sh
flent rrul -H <server> -t before -o before.png
# apply CAKE, then:
flent rrul -H <server> -t after -o after.png
```

The `bandwidth` value should be set 5–10% under **measured** throughput rather than the subscribed headline rate, then re-tested: set too high, the provider's buffer again becomes the bottleneck; set too low, throughput is surrendered without a latency return.

## Pitfalls

- **Shaper rate above the true bottleneck rate.** Loaded latency is unchanged after applying CAKE, because the downstream FIFO still fills first and the CAKE queue never accumulates the sojourn time CoDel needs to act on.
- **Shaping only the root egress qdisc.** Download latency remains bloated, because the queue in that direction sits in provider equipment and is reached only by pacing ingress through an IFB device.
- **Ignoring link-layer overhead on framed links.** A rate that looks safely below the subscribed figure still overruns the link, because ATM/PTM or Ethernet framing bytes are transmitted but not counted by a shaper configured without `ethernet`, `atm` or `docsis`.
- **Configuring against the advertised plan rate.** The shaper sits above the achievable rate whenever the line does not deliver its headline figure, reproducing the original symptom; the measured throughput is the only admissible input.
- **Omitting `nat` on a gateway.** One host running many parallel connections receives a share proportional to its flow count rather than a per-host share, because isolation hashes the post-translation tuple.
- **`tc qdisc add` on an interface that already has a root qdisc.** The command fails with a file-exists error rather than replacing the configuration; `tc qdisc replace` is the idempotent form.
- **Trusting a throughput speed test as validation.** Throughput can be unchanged or improved while latency under load remains catastrophic, because the bloated buffer costs delay, not bandwidth.
