---
title: "Off-CPU Analysis: Profiling Time Threads Spend Blocked"
date: 2026-08-15
track: linux-tools
summary: "A CPU flame graph shows where threads burn cycles and nothing about the 200 ms spent parked in a mutex or waiting on NFS. Off-CPU analysis traces scheduler context switches to attribute blocked time to stack traces: offcputime from bcc, a readable bpftrace equivalent, off-CPU flame graphs, and the overhead arithmetic that decides whether the tool can run on a busy machine."
reading_time: 6
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

**Gist.** Timer-based CPU profiling samples only threads that are currently running, so a thread parked in `futex_wait` or `io_schedule` is invisible to it and a latency-bound service can present a clean CPU profile. Off-CPU analysis closes the gap by tracing scheduler context switches: on switch-out a timestamp is recorded, on switch-in the elapsed interval is summed into a map keyed by the thread's stack traces. The cost is that the instrumentation fires per scheduler event rather than at a fixed sampling rate, so its overhead grows with the context-switch rate of the machine under study.

The [perf flame graph pipeline](/articles/linux-tools/2026-07-26-perf-flame-graphs/) has a structural blind spot: `perf record` samples threads that are *running*. A blocked thread is not on a CPU when the sampling timer fires, so it contributes nothing. Brendan Gregg's term for the complement is **off-CPU analysis**. CPU time plus off-CPU time accounts for all of a thread's wall-clock time, so both halves are needed to explain a latency number.

## What "off-CPU" means mechanically

Each time the kernel scheduler switches a thread off a CPU it fires the `sched:sched_switch` tracepoint; the classic bcc tools instrument the same path through the `finish_task_switch` kernel function. The recipe has two halves and one invariant:

- On switch-**out**, store `nsecs` in a map keyed by the outgoing thread's PID.
- On switch-**in**, look up that key, add `now − stored` to a histogram keyed by the thread's **user stack, kernel stack and command name**, and delete the start entry.

The invariant is that **every stored start timestamp must be consumed exactly once by the matching switch-in**, otherwise the start map grows without bound and intervals are double-counted. The `delete` after billing is what enforces it.

The two stacks answer different questions. The **kernel stack names the blocking mechanism** — `futex_wait_queue_me`, `io_schedule`, `epoll_wait`. The **user stack names the application code path** that reached it. Summation happens **in kernel space**, so only the aggregated map is copied to user space at the end. That aggregation is the reason eBPF made the technique practical where `perf record -e sched:sched_switch`, which writes every event to a capture file, largely was not.

## offcputime from bcc

The packaged tool is `offcputime` from bcc — `offcputime-bpfcc` on Debian and Ubuntu, `/usr/share/bcc/tools/offcputime` elsewhere. Folded output feeds directly into `flamegraph.pl`:

```bash
# 30 s of off-CPU time for one process, folded stacks, microseconds
offcputime-bpfcc -df -p "$(pgrep -nx mysqld)" 30 > out.stacks

flamegraph.pl --color=io --countname=us \
    --title="Off-CPU Time Flame Graph" < out.stacks > offcpu.svg
```

Flags documented in the man page that change what is measured rather than how it is printed: `-u` restricts to user threads and so excludes kernel worker threads; `-K` and `-U` collect kernel-only or user-only stacks; `-m MIN_BLOCK_TIME` and `-M MAX_BLOCK_TIME` bound the recorded interval in microseconds. **`-m 1000` discards blocks shorter than one millisecond**, which removes ordinary scheduler churn and retains intervals large enough to appear in a latency budget.

In the rendered graph, **width is blocked time, not sample count**. A wide `pthread_cond_wait` tower beneath a worker pool's task-fetch function represents idle workers; the same tower beneath a request handler represents contention. The stack frames distinguish the two cases; the width alone does not.

## The same measurement in bpftrace

The bpftrace program from *BPF Performance Tools* states the whole measurement in one screen, which makes the switch-out/switch-in pairing inspectable:

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

Two details carry the logic. `finish_task_switch` runs **in the context of the incoming thread** while its first argument is the outgoing `task_struct`, so a single probe body can perform both the stamp (`$prev->pid`) and the billing (`tid`). The `$1 == 0` branch makes the PID filter apply to stamping only, so a target-process filter never leaves unmatched start entries behind.

For a survey without stacks, in the spirit of the [bpftrace one-liners article](/articles/linux-tools/2026-07-25-bpftrace-one-liners/), `bpftrace -e 'tracepoint:sched:sched_switch { @[kstack(8)] = count(); }'` counts which kernel paths switch context most often.

## Off-CPU stacks do not identify the waker

An off-CPU stack ending in `futex_wait` establishes that a thread blocked on a lock. It does not establish which thread held that lock. That information lives on the other side of the scheduler. bcc's `wakeuptime` traces which threads *wake* blocked threads and with what stack, and `offwaketime` merges the two into a single folded stack rendered as an off-wake flame graph. The two halves meet at a `--` delimiter: **reading down from the delimiter gives the off-CPU stack, reading up gives the waker stack in reverse order**, which places the sleeping and waking functions adjacent. Chains can extend further, because the waker may itself have been blocked; Gregg calls the generalisation chain graphs — waker stacks stacked on waker stacks — and `offwaketime` resolves one hop.

## Overhead

CPU sampling at a fixed 99 Hz has a fixed cost per CPU. Off-CPU tracing costs per *event*, and scheduler events scale with load. Gregg puts the switch rate at over 100,000 and in extreme cases **1 million per second**, against a more typical production range of **20–50k/s**. His measured comparison ran on an 8-CPU Linux 4.15 system under a heavy MySQL load producing **102k context switches per second**: a 10-second perf-based trace cost about **9% throughput and produced a 224 MB capture file**, whose post-processing through `perf script` cost a further 13% for 35 seconds. In-kernel eBPF aggregation cost about **6%** during the trace, with a 13% spike for the first second and 13% across 6 seconds of post-processing; extending the trace from 10 s to 60 s moved post-processing only from 6 to 7 seconds.

That last figure is the shape of the difference: perf's cost after the run grows with the number of events captured, while eBPF's grows with the number of distinct map keys. The `cs` column of `vmstat 1` establishes the event rate on a candidate host beforehand, and `-p PID` together with `-m` reduces both the event volume and the key count. Published overhead figures are specific to Gregg's workload and hardware; the tool's own impact is measurable on the target directly.

Two interpretation caveats follow from what the tracepoint counts. First, off-CPU time includes **involuntary switches** — preemption on a saturated CPU — which are not blocking; `--state 2` restricts collection to `TASK_UNINTERRUPTIBLE`. Second, a whole-process profile is dominated by **idle pool workers**, whose wait towers are large and unrelated to request latency: Gregg reports such columns between 25 and 30 seconds wide. His remedy in the MySQL case is to filter the folded stacks to those passing through the request-handling function `do_command`, so only request-synchronous blocking remains.

## CPU vs off-CPU profiling

| | CPU flame graph | Off-CPU flame graph |
|---|---|---|
| Answers | where cycles go | where blocked time goes |
| Mechanism | timed sampling (`perf record -F 99`) | scheduler event tracing (eBPF) |
| Overhead | fixed per CPU | scales with context-switch rate |
| Width means | samples ≈ CPU time | microseconds blocked |
| Misses | all blocking: locks, I/O, waits | on-CPU spins, e.g. busy-wait locks |
| Failure modes | broken stacks, missing symbols | idle-pool noise, involuntary switches |

## Pitfalls

- **A whole-process off-CPU profile is dominated by an enormous condition-variable tower.** The cause is idle pool threads waiting for work; their blocked time is real but unrelated to request latency, and it must be excluded by stack filtering or by instrumenting request-synchronous context.
- **Blocked time exceeds the elapsed wall-clock duration of the trace.** Off-CPU time is summed across threads, so a process with many blocked threads accumulates more than one second of blocked time per second.
- **A large fraction of the profile has no obvious blocking syscall.** Involuntary preemption on a saturated CPU is counted as off-CPU time; `--state 2` limits collection to `TASK_UNINTERRUPTIBLE`.
- **The profiled process slows measurably during the trace.** The instrumentation fires on every context switch, so a host with a high `cs` rate in `vmstat 1` pays proportionally more than the ~6% Gregg measured at ~102k switches per second.
- **User stacks are truncated or show raw addresses.** The frame pointer is omitted or symbols are absent in the target binary; the kernel half of the stack is unaffected, which makes the blocking mechanism readable while the application path is not.
- **Sub-millisecond entries swamp the folded output.** Without `-m`, ordinary scheduler churn produces many distinct low-value stacks that enlarge the map and the resulting SVG.
- **An off-CPU stack ending in `futex_wait` is read as identifying the contended code.** It identifies the waiter only; the holder is visible through `offwaketime`.
