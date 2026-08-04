---
title: "bpftune: let eBPF retune your kernel so your sysctls don't go stale"
date: 2026-08-04
track: linux-tools
summary: "The sysctl values you set at boot were right for the load you had at boot. Traffic shifts, buffers saturate, congestion control stays wrong, and nobody ever revisits /etc/sysctl.d. Oracle's bpftune runs a low-overhead eBPF daemon that watches actual kernel behaviour and nudges tunables like tcp_rmem, somaxconn, and the congestion control algorithm as conditions change — and gets out of the way the moment you touch a setting by hand. Here's how to build it, run it, and read what it changed."
reading_time: 6
tags: [ebpf, bpf, bpftune, sysctl, tcp, autotuning, linux-tools]
sources:
  - title: "oracle/bpftune: bpftune uses BPF to auto-tune Linux systems"
    url: "https://github.com/oracle/bpftune"
  - title: "Introducing bpftune for lightweight, always-on auto-tuning of system behaviour — Oracle Linux Blog"
    url: "https://blogs.oracle.com/linux/introducing-bpftune"
  - title: "Auto-tuning the kernel — LWN.net"
    url: "https://lwn.net/Articles/998576/"
  - title: "Oracle Releases Updated bpftune For BPF-Based Auto-Tuning Of Linux Systems — Phoronix"
    url: "https://www.phoronix.com/news/Oracle-bpftune-0.4-1"
---

A modern Linux kernel exposes something on the order of 1,500 tunables, and almost all of them ship with a single static default. The ones that matter for a given workload get set once — in `/etc/sysctl.d`, a Chef recipe, an image build — using numbers that were correct for the traffic pattern in front of you *that day*. Then the workload moves. A service that was mostly small RPCs starts streaming large payloads, an instance is rehomed to a link with ten times the bandwidth-delay product, container density triples. The sysctl values don't move with it. `net.ipv4.tcp_rmem` that was generous at boot is now clamping throughput; a `somaxconn` sized for a quiet service drops connections during a spike. Nobody notices, because retuning is nobody's job and there's no signal telling you the values went stale.

bpftune is Oracle's answer to that gap: a daemon that uses eBPF to watch real kernel behaviour and adjust the relevant sysctls continuously, so the tuning tracks the workload instead of freezing at boot.

## The design philosophy is deliberately conservative

bpftune is not an aggressive optimizer, and that's the point. Its stated tenets, from the Oracle blog and the README, are worth internalizing before you run it:

- **Don't tune unless needed.** It avoids high-frequency tracing; the observability overhead has to stay negligible or the cure is worse than the disease. It reacts to events (buffer saturation, loss, namespace creation) rather than polling hot paths.
- **Keep out of the way.** If an administrator sets a tunable manually, bpftune detects that write and *stops* managing it. Your explicit choice always wins over the daemon's guess.
- **Explain every change.** Each adjustment is logged with what changed and why, so the system's behaviour never becomes a mystery.

Tuning is also bidirectional: bpftune raises a value to gain performance headroom, but pulls it back down when it sees the system approaching a limit — memory pressure, for instance — rather than ratcheting only upward.

## What it actually tunes

bpftune is built from dynamically linked plugins called *tuners*, each hooking a slice of kernel behaviour through BPF and receiving events over a ring buffer. The current set is network-heavy:

- **tcp_buffer** — auto-sizes TCP send/receive buffers by correlating buffer size against smoothed RTT, adjusting `net.ipv4.tcp_rmem` and `tcp_wmem` max values as flows demand more.
- **tcp_conn** (congestion control) — selects the congestion control algorithm based on observed conditions, e.g. switching toward BBR when loss climbs.
- **net_buffer** — core networking tunables including the listen backlog / `somaxconn` family.
- **neigh_table** — grows the neighbour (ARP/ND) table before it overflows.
- **route_table**, **ip_frag** — routing table sizing and IP fragmentation memory limits.
- **udp_buffer** — non-TCP buffer sizing.
- **netns** — detects network namespace creation and teardown so tuning is per-namespace, which matters on container hosts.
- **sysctl** — the meta-tuner that watches for manual sysctl writes and disables the tuner that would otherwise fight you.

On a plain laptop the LWN reviewer saw bpftune bump `net.ipv4.tcp_rmem` by roughly 25% within seconds of starting, then continue nudging it over the following minutes as it learned the workload.

## Build and install

bpftune builds against libbpf and needs BTF in the running kernel (`CONFIG_DEBUG_INFO_BTF=y`) plus BPF ring buffer support, which means roughly a 5.6+ kernel (5.4 on Oracle Linux). Install the toolchain, then build:

```bash
# Fedora/RHEL-family dependencies
sudo dnf install -y libbpf libbpf-devel libcap-devel bpftool \
    libnl3-devel clang llvm python3-docutils

git clone https://github.com/oracle/bpftune
cd bpftune
make
sudo make install        # installs bpftuned, the tuner plugins, and a systemd unit

# On some distros the plugin dir is lib not lib64:
#   make libdir=lib && sudo make install libdir=lib
```

Before committing to it, ask bpftune whether your kernel can support it fully:

```bash
$ bpftune -S
bpftune works fully
```

`-S` (`--support`) scans for BTF, ring buffer, and the BPF features each tuner needs, and reports whether support is full, partial (legacy mode), or none.

## Run it and watch what it does

Run in the foreground with logs to stderr while you're getting a feel for it:

```bash
sudo bpftune -s
```

`-s` (`--stderr`) sends log output to standard error instead of syslog. In steady state you'll run it as a service instead:

```bash
sudo systemctl enable --now bpftune
```

Either way, every adjustment is logged with a before/after. Tail it through journald:

```bash
$ journalctl -u bpftune -f
bpftuned[1123]: bpftune works in full mode
bpftuned[1123]: Applying tcp buffer tuner
bpftuned[1123]: Scenario 'need to increase TCP buffer size(s)' occurred for tunable
  'net.ipv4.tcp_rmem' in global ns. Need to increase buffer size(s) to maximize
  throughput
bpftuned[1123]: change net.ipv4.tcp_rmem(min default max) from (4096 131072 6291456)
  -> (4096 131072 7864320)
```

That log line is the whole value proposition in one place: which tunable, which namespace, the reason, and the exact old and new triple. When bpftune runs as a service the same lines land in syslog (`/var/log/messages` or `journalctl -u bpftune`); when it runs in the foreground it also prints a summary of everything it changed on exit.

## Inspecting and steering it

bpftune answers live queries over its `-q` (`--query`) interface, backed by a small local port. The queries that matter day to day:

```bash
bpftune -q tuners      # loaded tuners and their state (active / disabled)
bpftune -q tunables    # every sysctl the loaded tuners can touch
bpftune -q summary     # what has been changed so far
bpftune -q help        # list supported queries
```

`bpftune -q tuners` is where you confirm a tuner is actually active — a tuner drops to a disabled state if you've set its sysctl by hand, or if the kernel lacks a feature it needs.

There's no per-tuner on/off switch you flip in a config file, and that's intentional — bpftune's design goal is zero configuration. You disable a tuner in the way the daemon expects: **set its target sysctl manually.** Write `net.ipv4.tcp_rmem` in `/etc/sysctl.d` and reload, and the sysctl tuner sees the write and deactivates the TCP buffer tuner, leaving your value untouched. If you want to constrain what the daemon loads in the first place — testing a single tuner, say — use `-a`/`--allow` to permit only named plugins (e.g. `-a tcp_buffer_tuner.so`); the flag is repeatable.

Two more knobs are worth knowing. `-r`/`--learning_rate` (0–4, default 4) controls how aggressively the reinforcement-learning tuners explore alternatives such as congestion control algorithms; the default is usually right. And `-R`/`--rollback` restores the original sysctl values when the daemon exits, which is exactly what you want on a machine where you're evaluating bpftune and don't want it to leave state behind.

## When not to reach for it

LWN's caveat is fair: any tool that changes configuration without a human in the loop will eventually make a change you didn't want, at a moment you didn't expect. bpftune is a good fit for fleets of general-purpose hosts that nobody hand-tunes and where a bit more throughput is pure upside. It's a worse fit where predictability and reproducibility outrank performance — a benchmark rig, a latency-SLA'd box you've already tuned to the metal, anything where "the numbers changed themselves overnight" is an incident. For those, run it in `-R` mode to observe what it *would* do, read the log, and lift the good changes into your static config by hand.

**Try next:** run `sudo bpftune -s -R` on a busy host for an hour, then `bpftune -q summary` — compare its proposed `tcp_rmem`/`tcp_wmem` and congestion-control choices against whatever you've currently baked into `/etc/sysctl.d`.
