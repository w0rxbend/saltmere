---
title: "Off-CPU Analysis: Profiling the Time Your Threads Spend Blocked"
date: 2026-08-15
track: linux-tools
summary: "A CPU flame graph shows where threads burn cycles — and nothing about the 200 ms they spent parked in a mutex or waiting on NFS. Off-CPU analysis traces scheduler context switches to attribute blocked time to stack traces: offcputime from bcc, a bpftrace version you can read in full, off-CPU flame graphs, and the overhead math that decides whether you can run this on a busy box."
reading_time: 5
tags: [off-cpu, profiling, ebpf, bpftrace, bcc, scheduler, flame-graphs]
sources:
  - title: "Off-CPU Analysis (Brendan Gregg)"
    url: "https://www.brendangregg.com/offcpuanalysis.html"
  - title: "Off-CPU Flame Graphs (Brendan Gregg)"
    url: "https://www.brendangregg.com/FlameGraphs/offcpuflamegraphs.html"
  - title: "Linux eBPF Off-CPU Flame Graph (Brendan Gregg, 2016)"
    url: "https://www.brendangregg.com/blog/2016-01-20/ebpf-offcpu-flame-graph.html"
  - title: "offcputime-bpfcc(8) man page (Debian bpfcc-tools)"
    url: "https://manpages.debian.org/unstable/bpfcc-tools/offcputime-bpfcc.8.en.html"
  - title: "offcputime.bt — BPF Performance Tools book originals"
    url: "https://github.com/brendangregg/bpf-perf-tools-book/blob/master/originals/Ch06_CPUs/offcputime.bt"
---

The [perf flame graph pipeline](/articles/linux-tools/2026-07-26-perf-flame-graphs/) has a structural blind spot: `perf record` samples threads that are *running*. A thread parked in `futex_wait` or `io_schedule` never gets sampled, because it isn't on a CPU when the timer fires. So a service can show a clean, boring CPU profile while every request spends 300 ms blocked on a lock, a disk, or a downstream call. Brendan Gregg's term for the complement is **off-CPU analysis**: measure the time threads spend blocked and waiting, with the stack trace that led there. CPU time plus off-CPU time is 100% of thread time — you need both halves to explain a latency number.

## What "off-CPU" means mechanically

Every time the kernel scheduler switches a thread off a CPU, it fires the `sched:sched_switch` tracepoint (the classic bcc tools hook the same path via `finish_task_switch`). The recipe is: when a thread is switched *out*, record a timestamp; when it's switched back *in*, compute the delta and add it to a map keyed by the thread's user + kernel stack. The kernel stack tells you *how* it blocked (`futex_wait_queue_me`, `io_schedule`, `epoll_wait`); the user stack tells you *which code path* blocked. Sum in kernel via eBPF, and only the aggregated map crosses to userspace — this is why eBPF made off-CPU analysis practical where `perf record -e sched:sched_switch` (which logs every event to disk) mostly wasn't.

## offcputime from bcc

The packaged tool is `offcputime` from bcc — `offcputime-bpfcc` on Debian/Ubuntu, `/usr/share/bcc/tools/offcputime` elsewhere. Folded output feeds straight into flamegraph.pl:

```bash
# 30s of off-CPU time for one process, folded stacks, microseconds
offcputime-bpfcc -df -p "$(pgrep -nx mysqld)" 30 > out.stacks

flamegraph.pl --color=io --countname=us \
    --title="Off-CPU Time Flame Graph" < out.stacks > offcpu.svg
```

Useful flags from the man page: `-u` user threads only (skips kernel workers), `-K`/`-U` kernel-only or user-only stacks, and `-m MIN_BLOCK_TIME` / `-M MAX_BLOCK_TIME` in microseconds to filter. `-m 1000` is a good first pass: it drops sub-millisecond scheduler noise and keeps the blocks a human would call latency.

In the resulting flame graph, width is *blocked time*, not samples. Read it top-down from the blocking syscall: a wide `pthread_cond_wait` tower under your worker pool's `get_task` is idle threads (fine); the same tower under a request handler is contention (not fine).

## The same thing in readable bpftrace

The bcc tool is a black box; the bpftrace version from *BPF Performance Tools* fits on a screen and shows exactly what's measured:

```bash
bpftrace -e '
kprobe:finish_task_switch {
  $prev = (struct task_struct *)arg0;
  if ($1 == 0 || $prev->tgid == $1) {
    @start[$prev->pid] = nsecs;         // thread switched OUT: stamp it
  }
  $last = @start[tid];                  // thread switched IN: bill the gap
  if ($last != 0) {
    @usecs[kstack, ustack, comm] = sum((nsecs - $last) / 1000);
    delete(@start[tid]);
  }
}
END { clear(@start); }' 1234     # positional arg $1 = target PID, 0 = all
```

For a quick "why are we sleeping at all" survey without stacks, one line does it, in the spirit of the [bpftrace one-liners article](/articles/linux-tools/2026-07-25-bpftrace-one-liners/): `bpftrace -e 'tracepoint:sched:sched_switch { @[kstack(8)] = count(); }'` counts which kernel paths context-switch most.

## Off-CPU still doesn't tell you *why* — wakeups do

An off-CPU stack ending in `futex_wait` says "blocked on a lock." It doesn't say who held it. That's the waker's problem, and it needs the other half of the scheduler: bcc's `wakeuptime` traces who *wakes* blocked threads and with what stack, and `offwaketime` merges both into one folded stack — waker stack on top, blocked stack below — rendered as an **off-wake flame graph**. When lock contention is the mystery, this is the tool that names the culprit code path holding the lock. (Chains can go deeper — the waker was itself blocked on something — which Gregg calls chain graphs; `offwaketime`'s one hop resolves most real cases.)

## Overhead: the part that bites on busy boxes

CPU sampling at 99 Hz has fixed, tiny cost. Off-CPU tracing costs per *event*, and scheduler events scale with load — Gregg warns context switches can exceed a million per second in extreme cases. His measured example: at ~102k context switches/s, perf-based tracing cost ~9% throughput plus a 224 MB capture file, while in-kernel eBPF aggregation cost ~6% with nothing written until the end. Rules of thumb before running on production: check `vmstat 1`'s `cs` column first; keep durations short (10–30 s); use `-p PID` and `-m` thresholds to cut the event and map volume; expect worse on very high-cs boxes and measure the tool's own impact.

Two interpretation caveats. First, off-CPU time counts *involuntary* switches too (preemption when the CPU is saturated) — filter on state `TASK_UNINTERRUPTIBLE` (`--state 2`) to focus on genuine blocking. Second, the thread-pool problem: idle workers waiting for work dominate any whole-process off-CPU profile with enormous, boring wait towers. That's background noise, not request latency — the sharper (and more invasive) fix Gregg describes is instrumenting request-synchronous context, e.g. only counting blocked time inside MySQL's `do_command`.

## CPU vs off-CPU profiling

| | CPU flame graph | Off-CPU flame graph |
|---|---|---|
| Answers | where cycles go | where blocked time goes |
| Mechanism | timed sampling (`perf record -F 99`) | scheduler event tracing (eBPF) |
| Overhead | fixed, ~negligible | scales with context-switch rate |
| Width means | samples ≈ CPU time | microseconds blocked |
| Misses | all blocking: locks, I/O, waits | on-CPU spins, e.g. busy-wait locks |
| Watch out for | broken stacks, missing symbols | idle-pool noise, involuntary switches |

**Try next:** run `offcputime-bpfcc -df -m 1000 -p <pid> 30` against a service under load, render the SVG next to the same window's CPU flame graph, and check the two totals against wall-clock latency — then chase the widest lock wait with `offwaketime` to find the waker.
