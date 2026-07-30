---
title: "Taming Bufferbloat with the CAKE qdisc on Linux"
date: 2026-07-30
track: linux-tools
summary: "Why oversized buffers wreck latency under load, why pfifo_fast can't help, and how the CAKE qdisc combines a shaper, fq_codel-style AQM, and flow isolation into one line of tc."
reading_time: 6
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

You start a large upload, and suddenly a video call stutters, SSH keystrokes lag, and DNS lookups crawl. The link isn't saturated by anyone's fault in particular; you've simply hit **bufferbloat** — latency caused by packets sitting in a buffer that is far too large.

## What bufferbloat actually is

Somewhere between your machine and the internet there is a bottleneck: usually the slowest link, often your home uplink. When packets arrive faster than that link can drain them, they queue. Cheap memory made it tempting for router and modem vendors to give those queues enormous buffers — hundreds of milliseconds' worth. The logic was "a dropped packet is bad, so buffer more." The result is the opposite of good: a single bulk TCP flow fills the buffer, and every other packet now waits behind hundreds of milliseconds of someone else's data before it even reaches the wire.

Throughput looks fine on a speed test. Latency under load is catastrophic. Idle ping of 15 ms becomes 300 ms the moment an upload starts. That gap — idle latency versus latency while the link is busy — is the number that matters, and it's the one speed tests never show you.

## Why pfifo_fast / FIFO can't save you

The default qdisc on many Linux interfaces is `pfifo_fast`: a first-in-first-out queue with three priority bands. FIFO has one job — hold packets until the link is free — and it does nothing about *how many* it holds. If the buffer is big, FIFO happily keeps it full. Priority bands only reorder traffic that is *already in the same box*; they can't do anything about a buffer downstream in your modem, and they don't tell senders to slow down until the queue overflows and tail-drops. By then the damage is done and every flow backs off at once.

The insight from the bufferbloat.net / CeroWrt project (Dave Täht, Toke Høiland-Jørgensen, Jonathan Morton, and collaborators) was that you fix this two ways at once: **move the queue to a place you control and shape it just below the real bottleneck rate**, and **manage the queue actively** so it signals congestion early instead of only when it overflows.

## What CAKE does differently

CAKE — Common Applications Kept Enhanced — is the culmination of years of the SQM (Smart Queue Management) `htb + fq_codel` scripts, folded into a single qdisc. It merged into mainline Linux in **4.19** (October 2018); before that it lived as an out-of-tree module and shipped in OpenWrt/LEDE. It bundles four things:

- **A deterministic shaper.** CAKE rate-limits traffic itself, so the real bottleneck queue in your modem never fills. It's tighter and lower-overhead than HTB, and it can account for link-layer overhead (ATM/PTM framing on DSL, Ethernet overhead) so the shaped rate matches reality.
- **fq_codel-style AQM.** CoDel watches how long packets dwell in the queue and starts marking/dropping when *sojourn time* stays high, signalling TCP to slow down long before the buffer is full. CAKE's version has a tighter recovery algorithm and uses ECN when available.
- **Flow isolation.** By default CAKE uses `triple-isolate` with an 8-way set-associative hash, giving each flow (and, with `nat`, each host behind NAT) its own effective queue. A bulk upload can no longer starve a latency-sensitive flow — they're scheduled fairly.
- **Optional DiffServ "tins."** CAKE can sort traffic into priority classes based on DSCP marks (`diffserv3`, `diffserv4`, `diffserv8`, or `besteffort` to ignore marks). Unlike strict priority, tins get bandwidth guarantees, so a misbehaving high-priority flow can't monopolise the link.

The "Piece of CAKE" paper (Høiland-Jørgensen, Täht, Morton, 2018) makes the case that an improved queue algorithm alone isn't enough when the real buffer sits in legacy equipment you can't upgrade — hence the integrated shaper. That's the whole trick: you shape *just under* the ISP rate so the fast, well-managed CAKE queue becomes the bottleneck instead of the dumb one downstream.

## Applying it with tc

The one-liner most people want, on the egress (upload) interface:

```sh
# Set your bandwidth ~5-10% BELOW your measured upload rate
sudo tc qdisc add dev eth0 root cake bandwidth 100mbit
```

A more considered egress setup — internet RTT, NAT-aware host fairness, and link-layer accounting:

```sh
sudo tc qdisc replace dev eth0 root cake \
    bandwidth 100mbit rtt 100ms nat ethernet
```

Inspect what it's doing, including per-tin and drop/mark stats:

```sh
tc -s qdisc show dev eth0
```

To also control the *download* direction you have to shape ingress, which means redirecting incoming traffic through an Intermediate Functional Block (IFB) device — the buffer you're fighting is in your ISP's gear, so you can only manage it by pacing what you pull:

```sh
sudo modprobe ifb
sudo ip link add ifb0 type ifb 2>/dev/null || true
sudo ip link set ifb0 up
sudo tc qdisc add dev eth0 handle ffff: ingress
sudo tc filter add dev eth0 parent ffff: protocol all u32 \
    match u32 0 0 action mirred egress redirect dev ifb0
sudo tc qdisc add dev ifb0 root cake bandwidth 40mbit besteffort
```

Remove it all when you're done testing:

```sh
sudo tc qdisc del dev eth0 root
```

A few options worth knowing:

| Option | Effect |
| --- | --- |
| `bandwidth RATE` | Shaper rate — set below the true bottleneck |
| `rtt TIME` | Tunes CoDel's target; `internet` is the default |
| `nat` | Isolate by real host behind NAT, not NAT'd tuple |
| `diffserv4` / `besteffort` | Honour DSCP tins, or ignore marks entirely |
| `ack-filter` | Thin redundant TCP ACKs on asymmetric links |
| `ethernet` / `atm` / `docsis` | Link-layer overhead compensation keyword |

## Measuring before and after

Prove it, don't trust it. The simplest test: flood the link and ping through it.

In one terminal, saturate the uplink:

```sh
iperf3 -c <server> -t 60        # add -R to test download
```

In another, watch latency while it runs:

```sh
ping -i 0.2 1.1.1.1
```

Run it with the default qdisc first: you'll typically see idle pings of ~15 ms balloon to hundreds of ms during the transfer. Apply CAKE with a sensible `bandwidth`, repeat, and the loaded ping should stay within a few ms of idle — that's bufferbloat gone.

For a rigorous, repeatable picture, use **flent** (the FLExible Network Tester, also from this community), whose `rrul` test hammers the link in both directions while plotting latency:

```sh
flent rrul -H <server> -t before -o before.png
# apply CAKE, then:
flent rrul -H <server> -t after -o after.png
```

The two plots side by side are the clearest way to see the collapse of latency-under-load. Set your `bandwidth` value 5–10% under your *measured* throughput (not the plan's headline number), and re-test — too high and the ISP buffer wins again; too low and you leave throughput on the table.

**Try next:** measure your idle vs. loaded latency with the `iperf3` + `ping` combo above, then dial in a CAKE `bandwidth` on your egress interface and watch the loaded number drop.
