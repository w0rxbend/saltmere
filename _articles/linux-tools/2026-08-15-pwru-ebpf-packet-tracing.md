---
title: "pwru: Tracing a Packet's Path Through the Kernel With eBPF"
date: 2026-08-15
track: linux-tools
summary: "A ping goes out, nothing comes back, and tcpdump on both ends tells you nothing because the packet dies inside the kernel. pwru attaches eBPF kprobes to every kernel function that touches an skb, filters by a pcap expression, and prints the exact function where your packet was dropped. Here's how to read it, using pwru v1.0.12."
reading_time: 6
tags: [pwru, ebpf, networking, kernel, cilium]
sources:
  - title: "cilium/pwru — README (usage, flags, requirements)"
    url: "https://github.com/cilium/pwru/blob/main/README.md"
  - title: "cilium/pwru — Releases"
    url: "https://github.com/cilium/pwru/releases"
  - title: "man 7 pcap-filter — packet filter syntax"
    url: "https://www.tcpdump.org/manpages/pcap-filter.7.html"
  - title: "Tracing network packets with eBPF and pwru — sFlow blog"
    url: "https://blog.sflow.com/2025/07/tracing-network-packets-with-ebpf-and.html"
---

Here is a debugging dead end everyone hits eventually: a packet leaves the application, `tcpdump` sees it on the wire going out, and it never comes back — or the reverse, it arrives on the NIC and the application never receives it. tcpdump taps a couple of fixed points; everything between them (routing, netfilter, conntrack, the qdisc, a bridge, a veth) is a black box, and *that* is where the packet actually died. **pwru** — "packet, where are you?", a Cilium project — opens the box. It attaches eBPF **kprobes to every kernel function that takes an `sk_buff`**, filters by a pcap-style expression, and prints each function your packet passes through in order. The last line before it vanishes is your drop site. The current release is **v1.0.12** (July 2025).

## How it works and what it needs

The Linux network stack is hundreds of functions that pass an `skb` (socket buffer) pointer around: `ip_rcv`, `nf_hook_slow`, `__netif_receive_skb`, `tcp_v4_rcv`, `kfree_skb_reason`, and so on. pwru reads the kernel's **BTF** type information to find every function whose signature contains an `skb`, attaches a probe to each, and in-kernel runs your pcap filter against the packet the `skb` holds. Only matching packets emit an event, so the overhead is bounded even though thousands of probes are live.

Requirements are modest but real:

- **Kernel ≥ 5.3** to run at all; **≥ 5.9** for `--output-skb`; **≥ 5.18** for the faster `--backend=kprobe-multi` (it auto-detects).
- **BTF** must be present: `CONFIG_DEBUG_INFO_BTF=y` (check `/sys/kernel/btf/vmlinux` exists), plus `CONFIG_KPROBES`, `CONFIG_PERF_EVENTS`, `CONFIG_BPF`, `CONFIG_BPF_SYSCALL`. The kprobe-multi backend also wants `CONFIG_FUNCTION_TRACER` and `CONFIG_FPROBE`.
- **debugfs** mounted at `/sys/kernel/debug` (`mount -t debugfs none /sys/kernel/debug` if it's empty).
- Root (or `CAP_BPF` + `CAP_PERFMON`).

## Running it

The positional argument is a **pcap-filter expression** — the same language as `tcpdump` (`man 7 pcap-filter`), so `host`, `dst host`, `tcp`, `port`, `and`/`or` all work. (Older pwru had per-field flags like `--filter-dst-ip`; current versions fold all of that into the pcap expression, which is more expressive.)

```bash
# every skb touching 1.1.1.1
sudo pwru 'host 1.1.1.1'

# narrow to outbound HTTP to a specific host
sudo pwru 'dst host 1.1.1.1 and tcp dst port 80'
```

If you have no local binary, the container image carries everything:

```bash
docker run --privileged --rm -t --pid=host \
  -v /sys/kernel/debug/:/sys/kernel/debug/ \
  cilium/pwru pwru 'host 1.1.1.1'
```

`--output-tuple` and `--output-meta` are **on by default**, so a bare run already gives you the useful columns. The header:

```
SKB       CPU PROCESS       NETNS      MARK/x  IFACE  PROTO MTU  LEN  TUPLE                          FUNC
0xffff... 3   :1234         4026531840 0            eth0 0x0800 1500 60 1.2.3.4:44231->1.1.1.1:80(tcp) ip_output
```

Reading the columns: **SKB** is the buffer address (the same packet keeps the same address as it flows, so you follow one packet down the list); **CPU** and **PROCESS** are where the probe fired; **NETNS / MARK / IFACE / PROTO / MTU / LEN** are skb metadata from `--output-meta`; **TUPLE** is `src:port->dst:port(proto)` from `--output-tuple`; **FUNC** is the kernel function this line represents. Read top to bottom and you have the packet's itinerary.

## Finding the drop

The point of all this is the last function. A packet that's dropped ends its journey in `kfree_skb` / `kfree_skb_reason` or inside a netfilter hook (`nf_hook_slow`). To see the drop *and how it got there*, add a kernel stack:

```bash
sudo pwru --output-stack 'dst host 1.1.1.1 and tcp dst port 80'
```

The stack under a `kfree_skb_reason` line names the caller — a conntrack verdict, a routing failure, a full qdisc — which is usually the actual bug. Two more flags earn their keep:

- **`--filter-track-skb`** follows the *same logical packet* even after it's transformed — NAT rewrites the tuple, a tunnel encapsulates it — so you don't lose it at the point where its 5-tuple changes.
- **`--filter-func '.*'` / `--filter-func kfree_skb.*`** restricts which functions are probed (exact match or RE2 regex), handy to cut noise once you know roughly where to look.

## When it fits, and when it doesn't

pwru is the right tool when a packet **disappears inside the host** and you need the exact function or netfilter hook responsible — the class of bug where `tcpdump` shows you the entrance and exit but nothing in between. It is not a throughput profiler (that's `perf` or flame graphs) and not a policy enforcer (that's Tetragon); it's a surgical "where did this one go" tracer. The costs are honest: it needs a BTF-enabled kernel and root, attaching thousands of kprobes adds startup latency and a bounded per-packet cost while running, and on a very busy production box you'll want a tight pcap filter so you're not matching half your traffic. But for the specific misery of a silently dropped packet, nothing else shows you the exact line of kernel code where it died.

**Try next:** add an `iptables -A OUTPUT -p tcp --dport 80 -d 1.1.1.1 -j DROP` rule, then run `sudo pwru --output-stack 'dst host 1.1.1.1 and tcp dst port 80'` and `curl` that host — the trace ends at `kfree_skb_reason` with a stack pointing at the netfilter hook, the drop pinpointed. Remove the rule afterward.
