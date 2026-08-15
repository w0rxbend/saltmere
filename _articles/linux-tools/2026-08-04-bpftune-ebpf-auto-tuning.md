---
title: "bpftune: eBPF-driven continuous retuning of kernel sysctls"
date: 2026-08-04
track: linux-tools
summary: "Sysctl values set at boot are correct for the load present at boot. Traffic shifts, buffers saturate, congestion control stays wrong, and /etc/sysctl.d is never revisited. Oracle's bpftune runs a low-overhead eBPF daemon that observes kernel behaviour and adjusts tunables such as tcp_rmem, somaxconn and the congestion control algorithm as conditions change, and stops managing any tunable that is set by hand. This covers building it, running it, and reading what it changed."
reading_time: 7
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

**Gist.** A modern Linux kernel exposes a large number of tunables — the bpftune README counts 1,624 sysctls on a 6.2 kernel — nearly all shipping with a single static default, and the handful that matter for a workload are set once and then left to go stale as the traffic pattern moves. bpftune is a daemon that attaches extended Berkeley Packet Filter (eBPF) programs to kernel events, infers from those events that a tunable is constraining the system, and rewrites it in place. The cost is that machine state changes without a human in the loop: configuration drifts from whatever is recorded in `/etc/sysctl.d`, and reproducing a past run means reconstructing a sequence of daemon decisions rather than reading a file.

## The staleness problem

Tuning is normally applied once — in `/etc/sysctl.d`, a configuration-management recipe, an image build — using numbers correct for the traffic pattern of that day. The workload then moves. A service dominated by small remote procedure calls begins streaming large payloads; an instance is rehomed to a link with a much larger bandwidth-delay product; container density rises. The sysctl values do not move with it. A `net.ipv4.tcp_rmem` maximum that was generous at boot clamps throughput; a `somaxconn` sized for a quiet service drops connections during a spike. **No signal exists that reports a tunable has become the binding constraint**, so nothing prompts a revisit.

## Stated design tenets

The Oracle blog post and the project README state four tenets, and each constrains what the daemon may do.

- **Do not tune unless needed.** bpftune avoids high-frequency tracing; it reacts to discrete events — buffer saturation, loss, namespace creation — rather than polling hot paths. The stated intent is that observability overhead remains negligible.
- **Keep out of the way.** When an administrator writes a tunable manually, bpftune detects the write and **stops managing that tunable**. The explicit setting wins over the daemon's inference.
- **Explain every change.** Each adjustment is logged with the tunable, the scenario that triggered it, and the old and new values.
- **Do not replace tunables with more tunables.** The daemon is stated to be zero-configuration: no options are required, and the README says unexplained constants are avoided where possible.

The README describes adjustment as bidirectional rather than a ratchet: a value is raised to gain headroom and pulled back down when the daemon observes the system approaching a limit such as memory pressure.

## Tuners and the event path

bpftune is assembled from dynamically linked plugins called *tuners*. Each attaches BPF programs to a slice of kernel behaviour and receives events in user space over a **BPF ring buffer**. The current set is network-heavy:

- **tcp_buffer** — sizes TCP send and receive buffers by correlating buffer size against smoothed round-trip time (RTT), adjusting the maximum fields of `net.ipv4.tcp_rmem` and `tcp_wmem`.
- **tcp_conn** — selects the congestion control algorithm from observed conditions, for example moving toward BBR as loss climbs.
- **net_buffer** — core networking tunables including the listen backlog and `somaxconn` family.
- **neigh_table** — grows the neighbour (Address Resolution Protocol / Neighbor Discovery) table before it overflows.
- **route_table** and **ip_frag** — routing table sizing and IP fragmentation memory limits.
- **udp_buffer** — non-TCP buffer sizing.
- **netns** — detects network namespace creation and teardown, so tuning is applied per namespace. This is what makes the daemon usable on container hosts, where a single global value would be wrong for most namespaces.
- **sysctl** — the meta-tuner that watches for manual sysctl writes and disables the tuner that would otherwise contend with the administrator.

The LWN review reports that on the author's machine bpftune increased `net.ipv4.tcp_rmem` by 25% almost immediately, by a further 25% a few minutes later, and twice more about fifteen minutes after that.

## Build prerequisites

bpftune builds against libbpf and requires **BPF Type Format (BTF) in the running kernel** (`CONFIG_DEBUG_INFO_BTF=y`) together with BPF ring buffer support. That combination puts the floor at roughly a 5.6 kernel, or 5.4 on Oracle Linux where the features are backported.

```bash
# Fedora/RHEL-family dependencies
sudo dnf install -y libbpf libbpf-devel libcap-devel bpftool \
    libnl3-devel clang llvm python3-docutils

git clone https://github.com/oracle/bpftune
cd bpftune
make
sudo make install        # installs the bpftune binary, the tuner plugins, and a systemd unit

# On some distributions the plugin directory is lib rather than lib64:
#   make libdir=lib && sudo make install libdir=lib
```

The support probe reports whether the running kernel satisfies what each tuner needs:

```bash
$ bpftune -S
bpftune works fully
```

`-S` (`--support`) scans the system for the BPF features bpftune needs and reports the level found: the README shows `bpftune works fully` and, on a system with less support, `bpftune works in legacy mode`. The same probe reports separately whether per-network-namespace policy is available (it depends on the netns cookie). Legacy mode means reduced capability rather than that the daemon fails to start.

## Running and reading the log

Foreground operation with logs on standard error is the mode for initial observation:

```bash
sudo bpftune -s
```

`-s` (`--stderr`) directs log output to standard error instead of syslog. Steady-state operation uses the installed unit:

```bash
sudo systemctl enable --now bpftune
```

Every adjustment is logged with before and after values:

```bash
$ journalctl -u bpftune -f
bpftune[2778]: bpftune works fully
bpftune[2778]: Scenario 'specify bbr congestion control' occurred for tunable
  'TCP congestion control' in global ns.
bpftune[2778]: Due to need to increase max buffer size to maximize throughput
  change net.ipv4.tcp_rmem(min default max) from (4096 131072 6291456)
  -> (4096 131072 7864320)
```

The lines carry four pieces of state: **the tunable, the namespace, the scenario name that fired, and the exact old and new triple**. Under the service unit these lines reach syslog (`/var/log/messages`, or `journalctl -u bpftune`).

## Querying and constraining the daemon

bpftune answers live queries over its `-q` (`--query`) interface, backed by a local port:

```bash
bpftune -q tuners      # loaded tuners and their state (active / disabled)
bpftune -q tunables    # every sysctl the loaded tuners can touch
bpftune -q summary     # changes made so far
bpftune -q help        # supported queries
```

`bpftune -q tuners` is the check that a tuner is active. **A tuner can be inactive for two distinct reasons**: its target sysctl was written manually, or the kernel lacks a BPF feature it requires. Both leave the tuner not tuning, so a tuner absent on an older kernel is easily read as one deliberately overridden.

There is no per-tuner on/off switch in a configuration file; the design goal is zero configuration. Disabling a tuner is done through the mechanism the daemon already watches: **set its target sysctl manually**. Writing `net.ipv4.tcp_rmem` in `/etc/sysctl.d` and reloading causes the sysctl tuner to observe the write and deactivate the TCP buffer tuner, leaving the administrator's value in place. To constrain what loads at startup — testing a single tuner, for instance — `-a`/`--allow` permits only named plugins (`-a tcp_buffer_tuner.so`), and the flag is repeatable.

Two further options matter. `-r`/`--learning_rate` (range 0–4, default 4) sets the step size of an adjustment relative to the relevant limit: the manual page documents 0 as changing tunables by or within 1.0625% of the limit and 4 as 25%, so lower values are more conservative. `-R`/`--rollback` restores the original sysctl values when the daemon exits, which bounds the blast radius on a machine where bpftune is under evaluation.

## Where the fit is poor

The LWN caveat holds: a tool that changes configuration without a human in the loop will eventually make an unwanted change at an unexpected moment. bpftune fits fleets of general-purpose hosts that nobody hand-tunes and where additional throughput carries no downside. It fits poorly where predictability and reproducibility outrank throughput — a benchmark rig, a host already tuned against a latency service-level agreement, any environment in which unexplained overnight configuration change constitutes an incident. In those cases `-R` mode permits observing the proposed changes, after which the useful ones can be lifted into static configuration by hand.

## Pitfalls

- **Rebuilding without `libdir=lib` on a distribution that uses `lib`** installs the tuner plugins where the daemon does not look; bpftune starts, loads no tuners, and appears inert.
- **A kernel built without `CONFIG_DEBUG_INFO_BTF`** does not expose `/sys/kernel/btf/vmlinux`, so `bpftune -S` reports reduced or absent support; the daemon can then run in legacy mode, or run while tuning nothing.
- **Setting a sysctl in `/etc/sysctl.d` after bpftune has already raised it** deactivates that tuner permanently for the boot, freezing the value at whatever was written manually — the intended behaviour, but it silently ends the auto-tuning that was previously observed working.
- **Omitting `-R` during evaluation** leaves adjusted sysctl values in place after the daemon exits, so the machine's post-test state is neither the boot configuration nor the tuned one.
- **Reading `-q tuners` output as a health check** conflates a tuner inactive because of a manual override with one inactive for a missing kernel feature; neither is tuning anything.
- **Restricting the loaded set with `-a` on a container host** without including the netns tuner removes the component that tracks namespace creation and teardown, so per-namespace tuning is not in effect.
