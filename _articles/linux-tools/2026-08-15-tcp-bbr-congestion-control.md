---
title: "TCP BBR: model-based congestion control you can enable today"
date: 2026-08-15
track: linux-tools
summary: "CUBIC infers congestion from packet loss, which means it either fills every buffer on the path (bufferbloat) or collapses on links where loss isn't congestion. BBR instead builds an explicit model of the bottleneck — bandwidth times RTT — and paces to it. Two sysctls turn it on; ss -ti shows you the model it built. Also: why BBRv1 was accused of bullying CUBIC, and where BBRv3 actually stands in 2026."
reading_time: 5
tags: [tcp, bbr, congestion-control, networking, bufferbloat, sysctl]
sources:
  - title: "tcp: BBR congestion control algorithm — patch series (LWN.net)"
    url: "https://lwn.net/Articles/701177/"
  - title: "google/bbr — official BBR repository (v3 branch)"
    url: "https://github.com/google/bbr"
  - title: "draft-cardwell-ccwg-bbr — BBR Congestion Control (IETF)"
    url: "https://datatracker.ietf.org/doc/html/draft-cardwell-ccwg-bbr"
  - title: "Google's BBRv3 TCP Congestion Control Will Be Upstreamed To Linux (Phoronix)"
    url: "https://www.phoronix.com/news/Google-BBRv3-Linux"
  - title: "BBR TCP — ESnet Fasterdata host tuning guide"
    url: "https://fasterdata.es.net/host-tuning/linux/recent-tcp-enhancements/bbr-tcp/"
---

Linux's default congestion control, **CUBIC**, belongs to the loss-based family: push more packets until one is dropped, back off, repeat. That heuristic has two failure modes baked in. On a path with **deep buffers**, the drop only happens after every queue is full, so CUBIC *operates* at maximum queue depth — that standing queue is **bufferbloat**, and it taxes every other flow's latency. On a path with **random loss** — WiFi, cellular, a long transatlantic link with a 0.1% loss floor — CUBIC treats every stray drop as congestion and halves its window, so a 10 Gbit pipe delivers a trickle. The [cake qdisc article](/articles/linux-tools/2026-07-30-cake-qdisc-bufferbloat) attacked bufferbloat from the router side; **BBR** attacks it from the sender side.

## The model instead of the heuristic

BBR (Bottleneck Bandwidth and Round-trip propagation time), from Google and merged in **Linux 4.9** (2016), doesn't wait for loss. It continuously estimates two numbers that fully describe the pipe:

- **BtlBw** — bottleneck bandwidth, the windowed *max* of recent delivery rate samples.
- **RTprop** — the propagation RTT, the windowed *min* of recent RTT samples (the RTT with no queue standing).

Their product is the **bandwidth-delay product** — exactly the amount of data that fits in the path with zero queue. BBR paces transmission at BtlBw and caps data in flight near the BDP, cycling through phases (STARTUP, DRAIN, PROBE_BW, PROBE_RTT) that periodically probe for more bandwidth and periodically drain the queue to re-measure the true RTT. Loss is (in v1, literally) not part of the model. The payoff: on lossy long-fat paths BBR routinely sustains **orders of magnitude** more throughput than CUBIC, while keeping the bottleneck queue — and therefore RTT — short.

| | CUBIC (loss-based) | BBR (model-based) |
|---|---|---|
| Congestion signal | packet loss | measured bandwidth + RTT |
| Steady state | buffer full, then drop | ~1 BDP in flight, short queue |
| Random-loss link | throughput collapses | mostly unaffected |
| Deep-buffer link | bufferbloat | low standing latency |
| Shallow-buffer sharing | polite | v1: aggressive vs. CUBIC |

## Enabling it

BBR ships as a module in every mainstream distro kernel. Pair it with the **fq** qdisc — BBR is a *pacing* algorithm, and fq implements per-flow pacing in the qdisc layer (since 4.13 BBR can fall back to internal TCP-layer pacing, but fq is the recommended pairing):

```bash
modprobe tcp_bbr
sysctl -w net.core.default_qdisc=fq
sysctl -w net.ipv4.tcp_congestion_control=bbr

# verify
sysctl net.ipv4.tcp_available_congestion_control
sysctl net.ipv4.tcp_congestion_control
```

Persist via `/etc/sysctl.d/90-bbr.conf`. This affects **outbound** connections from this host — it's a sender-side change, which is why it's popular on servers pushing data to far-away or lossy clients.

## Measuring what it does

`iperf3` can select the algorithm per test, so you can A/B on the same path:

```bash
iperf3 -c remote-host -C cubic -t 30
iperf3 -c remote-host -C bbr   -t 30
```

On a clean LAN the numbers will tie. Add distance or loss (or emulate it: `tc qdisc add dev eth0 root netem loss 1% delay 40ms`) and BBR pulls away. To see BBR's model live on a real connection, `ss -ti` prints the internal state per socket:

```
bbr:(bw:9.2Gbps,mrtt:4.3ms,pacing_gain:1.25,cwnd_gain:2)
pacing_rate 11.4Gbps delivery_rate 9.1Gbps ...
```

`bw` is the BtlBw estimate, `mrtt` the min-RTT, and the gains tell you which phase the flow is in. Watching `mrtt` stay flat while a CUBIC flow's RTT balloons is the whole argument in one line.

## The fairness fight, and where v3 stands

BBRv1's clean model has a dirty secret: because it ignores loss entirely, a BBRv1 flow sharing a **shallow-buffered** bottleneck with CUBIC flows keeps sending through their loss signals and can take a grossly unfair share — measurement studies showed BBRv1 claiming a fixed ~40% of such links regardless of how many CUBIC flows competed, plus elevated retransmit rates from the loss it shrugged off. **BBRv2** answered by folding loss and **ECN** signals back into the model as guardrails; **BBRv3** (presented at IETF 117, 2023) fixed further bugs and is what Google deploys fleet-wide for google.com and YouTube traffic, reporting ~12% fewer retransmits than v2.

The upstream status deserves honesty, because blog posts routinely get it wrong: **mainline Linux still ships BBRv1** as `tcp_bbr` in 2026. Google said in 2023 it intended to upstream v3, but the code lives in the [google/bbr](https://github.com/google/bbr) repository's `v3` branch (periodically rebased onto recent kernels, e.g. v6.13.x) and in patched distro kernels like XanMod and Zen — not in `net-next`. If a tutorial claims `sysctl` gave you "BBRv3" on a stock kernel, it didn't.

## Should you switch?

BBR is a clear win for senders pushing bulk data over long-RTT or lossy paths — CDN edges, cross-region replication, download servers with mobile clients — and it self-inflicts far less latency than CUBIC on buffered links. Skip it (or test carefully) when your traffic mostly competes with CUBIC flows across a shallow-buffered bottleneck you also care about, and don't expect miracles inside a low-latency datacenter, where CUBIC was never the problem. It's per-namespace tunable, so you can enable it for one container's egress before touching the host default.

**Try next:** run the `iperf3 -C cubic` vs `-C bbr` comparison through `netem` with `loss 1% delay 40ms` on a veth pair, watching `ss -ti` in a loop on the sender — you should see CUBIC's cwnd sawtooth and BBR's steady `bw` estimate side by side.
