---
title: "TCP BBR: model-based congestion control"
date: 2026-08-15
track: linux-tools
summary: "CUBIC infers congestion from packet loss, so it either fills every buffer on the path (bufferbloat) or collapses on links where loss is not congestion. BBR instead builds an explicit model of the bottleneck — bandwidth times round-trip propagation time — and paces to it. Two sysctls enable it; ss -ti prints the model it built. Includes why BBRv1 was accused of crowding out CUBIC, and where BBRv3 stands in 2026."
reading_time: 6
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

**Gist.** Loss-based congestion control treats a dropped packet as the only evidence of congestion, which forces the sender to fill the bottleneck queue before it learns anything, and misreads non-congestive loss as overload. BBR (Bottleneck Bandwidth and Round-trip propagation time) replaces that inference with a measured model of the path — bottleneck bandwidth and minimum round-trip time (RTT) — and paces transmission to it. The cost is that a sender running a model which excludes loss can hold more than its share of a shallow-buffered bottleneck against loss-based flows, and that the model must be maintained by periodic probing phases that deliberately perturb the flow.

## Where the loss heuristic fails

Linux's default congestion control, **CUBIC**, belongs to the loss-based family: increase the sending rate until a packet is dropped, reduce the congestion window, repeat. Two failure modes follow directly from that rule.

On a path with **deep buffers**, a drop occurs only once a queue is full, so the steady state of a CUBIC flow *is* maximum queue depth. The resulting standing queue is **bufferbloat**: every packet, including those of latency-sensitive flows sharing the bottleneck, waits behind it. On a path with **non-congestive loss** — wireless links, cellular links, a long intercontinental path with a residual loss floor — CUBIC cannot distinguish a corrupted frame from an overflowing queue and reduces its window on both, so a high-capacity long-RTT pipe delivers a fraction of its capacity. The [cake qdisc article](/articles/linux-tools/2026-07-30-cake-qdisc-bufferbloat) addresses bufferbloat at the router; **BBR** addresses it at the sender.

## The two estimates and the invariant

BBR, developed at Google and merged in **Linux 4.9** (2016), does not wait for loss. It maintains two quantities that between them describe the pipe:

- **BtlBw** — bottleneck bandwidth, the windowed **maximum** of recent delivery-rate samples.
- **RTprop** — round-trip propagation time, the windowed **minimum** of recent RTT samples, that is, the RTT observed when no queue is standing.

Each is a windowed extremum rather than an average, and the choice of extremum is forced by what each measurement is contaminated by. Delivery rate can only be *understated* by an idle or application-limited sender, so the maximum is the least-corrupted sample. RTT can only be *overstated* by queueing delay, so the minimum is the least-corrupted sample. The two cannot be measured at the same instant: raising the rate to find BtlBw builds a queue that inflates RTT, and draining the queue to find RTprop requires sending below BtlBw. **The estimates are therefore acquired in alternation, not simultaneously.**

Their product is the **bandwidth-delay product (BDP)** — the amount of data that fits in the path with an empty bottleneck queue. BBR paces transmission at BtlBw and caps data in flight near the BDP. Operating at that point is the invariant: the pipe is full and the queue is not.

## The phase machine

BBR cycles through four states, each defined by a gain applied to the pacing rate and to the in-flight cap:

- **STARTUP** — the rate rises rapidly to discover BtlBw on a new connection, in the manner of slow start.
- **DRAIN** — the rate drops below BtlBw to remove the queue that STARTUP's overshoot created.
- **PROBE_BW** — the steady state. The pacing gain cycles above and below unity, briefly sending faster than BtlBw to test whether more bandwidth has appeared, then slower to return the queue to empty.
- **PROBE_RTT** — in-flight data is reduced sharply so the bottleneck queue drains and a fresh, uninflated RTprop sample can be taken.

PROBE_RTT is the visible cost of the model: **a flow that must periodically stop filling the pipe in order to re-measure the empty-queue RTT gives up throughput during that interval.** In BBRv1 the model contained no loss term at all.

| | CUBIC (loss-based) | BBR (model-based) |
|---|---|---|
| Congestion signal | packet loss | measured bandwidth and RTT |
| Steady state | buffer full, then drop | approximately 1 BDP in flight, short queue |
| Non-congestive loss | throughput falls sharply | largely unaffected |
| Deep-buffer link | bufferbloat | low standing latency |
| Shallow-buffer sharing | yields to competitors | v1: holds a disproportionate share |

## Enabling it

BBR ships as a module in mainstream distribution kernels. It is a **pacing** algorithm and is paired with the **fq** queueing discipline, which implements per-flow pacing in the qdisc layer. Since Linux 4.13 BBR can fall back to pacing inside the TCP layer, but fq remains the documented pairing:

```bash
modprobe tcp_bbr
sysctl -w net.core.default_qdisc=fq
sysctl -w net.ipv4.tcp_congestion_control=bbr

# verify
sysctl net.ipv4.tcp_available_congestion_control
sysctl net.ipv4.tcp_congestion_control
```

The settings persist via `/etc/sysctl.d/90-bbr.conf`. The change affects **outbound** connections from the host only — congestion control is a sender-side decision, which is why the setting is applied on servers pushing data toward distant or lossy clients. It is tunable per network namespace, so a single container's egress can be switched before the host default is.

## Observing the model

`iperf3` selects the algorithm per test, which permits an A/B comparison over one path:

```bash
iperf3 -c remote-host -C cubic -t 30
iperf3 -c remote-host -C bbr   -t 30
```

On a short, clean local path the two are expected to tie: with negligible RTT and no loss, CUBIC's heuristic is not being misled. Adding distance or loss — or emulating both with `tc qdisc add dev eth0 root netem loss 1% delay 40ms` — separates them. `ss -ti` prints the live per-socket model:

```
bbr:(bw:9.2Gbps,mrtt:4.3ms,pacing_gain:1.25,cwnd_gain:2)
pacing_rate 11.4Gbps delivery_rate 9.1Gbps ...
```

`bw` is the BtlBw estimate, `mrtt` the minimum RTT, and the gains identify the current phase. A `pacing_gain` above unity indicates a probing interval within PROBE_BW. **The diagnostic worth watching is `mrtt`: if it stays flat while a competing CUBIC flow's RTT grows, the queue is being kept short.**

## Fairness, and the version shipped upstream

Because BBRv1 excludes loss from its model, a BBRv1 flow sharing a **shallow-buffered** bottleneck with CUBIC flows continues sending through the loss signal that causes the CUBIC flows to back off. Measurement studies reported a BBRv1 flow holding a share of such a link that does not shrink as the number of competing CUBIC flows grows, together with elevated retransmission rates from the loss it ignored. **BBRv2** reintroduced loss and explicit congestion notification (ECN) signals into the model as bounds on the sending rate. **BBRv3** carries further fixes and is the version Google has described deploying on its own traffic; the sources cited here give no measured v3-versus-v2 comparison.

The upstream status is frequently misreported: **mainline Linux still ships BBRv1 as `tcp_bbr` in 2026.** Google stated in 2023 an intention to upstream v3, but the code resides in the `v3` branch of the [google/bbr](https://github.com/google/bbr) repository, periodically rebased onto recent kernels, and in patched distribution kernels such as XanMod and Zen — not in `net-next`. A `sysctl` on a stock kernel does not select v3.

## Applicability

The measured benefit concentrates on senders pushing bulk data over long-RTT or lossy paths: content-delivery edges, cross-region replication, download servers serving mobile clients. On such paths BBR sustains substantially higher throughput than CUBIC while holding the bottleneck queue, and therefore RTT, short. Inside a low-latency datacenter the loss heuristic was not the limiting factor, and no comparable gain should be assumed.

## Pitfalls

- Enabling `tcp_bbr` without setting `net.core.default_qdisc=fq` leaves pacing to the TCP layer fallback; on kernels before 4.13 there is no such fallback, and the burst behaviour is not what the model assumes.
- Setting `net.ipv4.tcp_congestion_control=bbr` on a host that only *receives* bulk data changes nothing measurable, because the algorithm governs the sender.
- Benchmarking BBR against CUBIC on a clean local link produces a tie and is read as "BBR does not work"; the heuristic BBR replaces only misfires under loss or long RTT.
- A BBRv1 sender sharing a shallow-buffered bottleneck with CUBIC traffic that also matters will hold a disproportionate share and raise retransmissions on that link.
- Treating `ss -ti` output as a throughput report conflates `pacing_rate` with achieved rate; `delivery_rate` is the measured figure, and `bw` is a windowed maximum, not an instantaneous value.
- Assuming a `sysctl`-enabled BBR on a distribution kernel is BBRv3: mainline ships v1, so the v2 and v3 loss and ECN bounds are absent unless a patched kernel is running.
- Reading a rise in `mrtt` as path degradation when it can equally indicate that the PROBE_RTT interval has not recently produced a fresh minimum sample.
