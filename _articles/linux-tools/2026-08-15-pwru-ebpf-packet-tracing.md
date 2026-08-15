---
title: "pwru: Tracing a Packet's Path Through the Kernel With eBPF"
date: 2026-08-15
track: linux-tools
summary: "A ping goes out, nothing comes back, and tcpdump at both ends reports nothing, because the packet dies inside the kernel. pwru attaches eBPF kprobes to every kernel function that takes an sk_buff, filters in-kernel by a pcap expression, and prints the function where the packet was freed. Reading that output."
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

**Gist.** A packet that leaves an application and never returns — or arrives at the network interface controller (NIC) and never reaches the socket — is invisible to `tcpdump`, which taps two fixed points and leaves routing, netfilter, connection tracking, the queueing discipline (qdisc), bridges and virtual Ethernet (veth) pairs as an unobserved interior. **pwru**, a Cilium project whose name abbreviates the question "packet, where are u?", attaches extended Berkeley Packet Filter (eBPF) **kprobes to every kernel function whose signature contains an `sk_buff` pointer**, evaluates a pcap-style filter expression in-kernel, and emits one event per matching packet per probed function, so the sequence of functions the packet traversed is printed in order and the last line before it disappears identifies the drop site. The cost is a BTF-enabled kernel, root privilege, startup latency proportional to the number of attached probes, and a per-packet cost at every probed function for the duration of the run.

## Mechanism

The Linux network stack is composed of hundreds of functions that pass a pointer to a socket buffer (`sk_buff`, conventionally `skb`) between them: `ip_rcv`, `nf_hook_slow`, `__netif_receive_skb`, `tcp_v4_rcv`, `kfree_skb_reason`, and so on. The `skb` is the kernel's representation of one packet in flight; it carries the packet bytes plus metadata such as the network namespace, the firewall mark, the ingress or egress interface, the protocol and the length.

pwru derives its probe set from that convention rather than from a hand-maintained list. It reads the kernel's **BPF Type Format (BTF)** — the self-describing type information the kernel exposes at `/sys/kernel/btf/vmlinux` — to enumerate function signatures, selects **every function that takes an `skb`**, and attaches a kprobe to each. Each probe runs the same compiled filter program: the supplied pcap expression is evaluated **in kernel context against the packet the `skb` holds**, and an event is written to userspace only when the expression matches. **Filtering before the event leaves the kernel is what bounds the overhead**, since thousands of probes may be live while only the packets of interest produce records.

The invariant that makes the output readable is that **an `skb` keeps its address for as long as the kernel keeps handling the same buffer**. The SKB column is therefore a join key: grouping lines by that address reconstructs the itinerary of one packet, and interleaving from other traffic and other CPUs can be filtered out after the fact.

That invariant has a limit, and pwru names it. When a packet is transformed — network address translation (NAT) rewrites the tuple, or a tunnel encapsulates the original packet inside a new one — the bytes no longer satisfy the pcap expression, so subsequent probes stop matching. **`--filter-track-skb` follows the same logical buffer past such a transformation**, which is the flag that keeps a trace from ending at the NAT hook rather than at the real drop.

## Requirements

- **Kernel ≥ 5.5** to run at all; **≥ 5.9** for `--output-skb`; **≥ 5.18** for the `kprobe-multi` backend, selected with `--backend=kprobe-multi` and auto-detected otherwise. The multi backend attaches the probe set in one operation rather than one kprobe at a time, which is where the startup latency of a full attach is paid.
- **BTF present**: `CONFIG_DEBUG_INFO_BTF=y`, verifiable by the existence of `/sys/kernel/btf/vmlinux`, plus `CONFIG_KPROBES`, `CONFIG_PERF_EVENTS`, `CONFIG_BPF` and `CONFIG_BPF_SYSCALL`. The kprobe-multi backend additionally requires `CONFIG_FUNCTION_TRACER` and `CONFIG_FPROBE`.
- **debugfs mounted at `/sys/kernel/debug`**; if the directory is empty, `mount -t debugfs none /sys/kernel/debug`.
- **Root privilege**, since the tool loads BPF programs and attaches kprobes.

Without BTF the probe set cannot be derived at all, so the failure is total rather than degraded: a kernel built without `CONFIG_DEBUG_INFO_BTF` cannot be traced by pwru regardless of its version.

## Invocation

The positional argument is a **pcap-filter expression**, the language documented in `man 7 pcap-filter` and shared with `tcpdump`: `host`, `dst host`, `tcp`, `port` and the `and`/`or` combinators all apply. Older pwru versions exposed per-field flags such as `--filter-dst-ip`; current versions express the same selection through the pcap expression.

```bash
# every skb touching 1.1.1.1
sudo pwru 'host 1.1.1.1'

# outbound HTTP to one host
sudo pwru 'dst host 1.1.1.1 and tcp dst port 80'
```

The container image carries the binary and its dependencies:

```bash
docker run --privileged --rm -t --pid=host \
  -v /sys/kernel/debug/:/sys/kernel/debug/ \
  cilium/pwru pwru 'host 1.1.1.1'
```

`--output-tuple` and `--output-meta` add the tuple and `skb` metadata columns to each line:

```
SKB       CPU PROCESS       NETNS      MARK/x  IFACE  PROTO MTU  LEN  TUPLE                          FUNC
0xffff... 3   :1234         4026531840 0            eth0 0x0800 1500 60 1.2.3.4:44231->1.1.1.1:80(tcp) ip_output
```

**SKB** is the buffer address and the packet's identity across lines. **CPU** and **PROCESS** record where the probe fired, which is not necessarily the sending process: a packet handled in softirq context is attributed to whatever was running on that CPU. **NETNS**, **MARK**, **IFACE**, **PROTO**, **MTU** and **LEN** are `skb` metadata supplied by `--output-meta`; a changing NETNS value is the signature of a packet crossing a veth pair into a container. **TUPLE** is `src:port->dst:port(proto)` from `--output-tuple`. **FUNC** is the probed function this line represents.

## Locating the drop

The diagnostic content is in the final function reached. A dropped packet ends in `kfree_skb` or `kfree_skb_reason`, or inside a netfilter hook (`nf_hook_slow`). The function name alone identifies the stage, not the reason; the kernel stack identifies the caller:

```bash
sudo pwru --output-stack 'dst host 1.1.1.1 and tcp dst port 80'
```

The stack beneath a `kfree_skb_reason` line names what invoked the free — a conntrack verdict, a routing failure, a full qdisc — and that caller is the finding. **`--filter-func`** restricts which functions are probed, by exact name or RE2 regular expression (`--filter-func 'kfree_skb.*'`), which reduces both event volume and attach time once the region of interest is known.

## Scope

pwru answers "where inside this host did this packet stop". It is not a throughput profiler — `perf` and flame graphs address that — and not a policy enforcement mechanism, which is Tetragon's role. The constraints are the ones listed above: BTF, root, an attach cost proportional to the probe set, and a per-packet cost at each probed function. On a busy host, a broad expression such as `host <busy-server>` matches a large fraction of traffic and the event stream becomes the bottleneck; narrowing the expression is what keeps the trace legible.

A reproducible exercise: install `iptables -A OUTPUT -p tcp --dport 80 -d 1.1.1.1 -j DROP`, run `sudo pwru --output-stack 'dst host 1.1.1.1 and tcp dst port 80'`, and issue a `curl` to that host. The trace terminates at `kfree_skb_reason` with a stack pointing at the netfilter hook. Remove the rule afterwards.

## Pitfalls

- **A kernel without `CONFIG_DEBUG_INFO_BTF` produces no trace at all**, not a partial one: the probe set is derived from BTF, so there is nothing to attach. `/sys/kernel/btf/vmlinux` missing is the check.
- **`--output-skb` on a kernel between 5.5 and 5.9 fails** while plain tracing works, because that flag has a higher minimum kernel version than the tool itself.
- **A trace that ends at a NAT or tunnel boundary without a free is not a drop.** The packet's tuple changed, later probes no longer match the pcap expression, and the trace stops; `--filter-track-skb` continues it.
- **Interleaved lines from unrelated packets look like an out-of-order path.** Several packets can match one expression concurrently on different CPUs; the itinerary is only well-defined per SKB address.
- **The PROCESS column can name an unrelated task.** Receive-side processing runs in softirq context, so the attributed process is the one occupying that CPU, not the socket owner.
- **A wide expression on a production host floods the event stream**, since the in-kernel filter bounds overhead only in proportion to how selective the expression is.
- **debugfs unmounted at `/sys/kernel/debug` prevents startup** even when every kernel configuration option is set; an empty directory is the symptom.
