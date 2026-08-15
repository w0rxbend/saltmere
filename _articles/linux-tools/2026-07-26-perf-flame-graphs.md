---
title: "Flame graphs: locating CPU time with perf"
date: 2026-07-26
track: linux-tools
summary: "perf record samples a running program's stack at a fixed frequency; a flame graph aggregates those samples into one picture where box width is proportional to on-CPU time. This covers the record-to-SVG pipeline and the two failure modes that produce broken stacks."
reading_time: 6
tags: [perf, flamegraph, profiling, cpu, linux-tools, performance]
sources:
  - title: "Linux perf Examples (Brendan Gregg)"
    url: "https://www.brendangregg.com/perf.html"
  - title: "brendangregg/FlameGraph README"
    url: "https://github.com/brendangregg/FlameGraph/blob/master/README.md"
  - title: "CPU Flame Graphs (Brendan Gregg)"
    url: "https://www.brendangregg.com/FlameGraphs/cpuflamegraphs.html"
  - title: "perf-record(1) man page"
    url: "https://man7.org/linux/man-pages/man1/perf-record.1.html"
  - title: "Linux perf Tutorial: CPU Profiling, Call Graphs, and debuginfod"
    url: "https://www.golinuxcloud.com/linux-perf-performance-analysis/"
---

**Gist.** Tracers such as `strace` and bpftrace report which events a process performs, but not which function consumes the processor. A statistical profiler answers that question by interrupting the central processing unit (CPU) at a fixed frequency, capturing the current stack trace, and treating the resulting sample counts as the profile; a flame graph renders the aggregated stacks as nested boxes whose width is proportional to sample count. The cost is that the result is a statistical estimate rather than an exact accounting, and that stack capture depends on symbol tables and on an unwinding method that many production binaries do not support.

## Sampling versus tracing

A tracer intercepts *every* occurrence of an event — a system call, or a userspace probe (uprobe) placed on each function entry. The measurement is exact, but the overhead scales with how often the event fires, so a hot function turns the profiler into the dominant cost.

`perf record` instead uses the kernel's `perf_events` subsystem to arm a timer — for example at 99 Hz — and inspects the stack only on those interrupts. **Overhead is bounded by the sample rate rather than by program behaviour**, which is the property that makes the tool usable on a live service. The information lost is per-call detail; the information kept is the statistical distribution of on-CPU time, which is what hotspot analysis requires.

The resolution follows directly from the arithmetic. Sampling at 99 Hz for 60 seconds yields roughly 5,900 samples per CPU, so a function occupying 40% of CPU time appears in roughly 40% of them. That is ample separation for identifying a dominant consumer, and correspondingly poor at distinguishing two functions that differ by a fraction of a percent — **a flame graph is evidence about large effects, not small ones**.

## The recording command

```bash
perf record -F 99 -a -g -- sleep 30
```

- `-F 99` — sample at 99 Hz. The frequency is deliberately not a round 100: an exact round number risks running in lockstep with periodic kernel or application activity (timers, scheduler ticks), which would bias samples toward whatever executes on that beat.
- `-a` — sample all CPUs system-wide rather than a single command.
- `-g` — record the call graph at each sample rather than only the leaf instruction pointer. Without it the profile is flat: it names the function executing but not the caller that led there.
- `-- sleep 30` — `sleep` is not the profiling target. It does nothing, so it serves only to bound system-wide sampling to 30 seconds. Replacing `-a` with `-p PID` restricts sampling to one process.

The run writes `perf.data` into the current directory. `perf report` then opens an interactive, `top`-like ncurses view sorted by overhead percentage, with per-function call graphs that expand on demand. For a multi-threaded service with deep stacks, that text tree contains hundreds of call paths and must be read sequentially; the flame graph exists to present the same aggregation in a single proportional image.

## Two ways stacks come back broken

Both failure modes present identically in `perf report`: `[unknown]` frames, or stacks truncated to a single entry.

**Missing symbols.** perf resolves sampled addresses to function names through the binary's symbol table. Stripped binaries, code generated at run time by a dynamic compiler, and optimized builds shipped without debug information all resolve to `[unknown]`. The remedy is to install the matching `-dbg`/`-dbgsym` package, or to use `debuginfod` where the distribution serves it, so that symbols are found out of band.

**Broken stacks: the frame-pointer problem.** By default `-g` walks the stack through frame pointers (`--call-graph fp`). That walk is only possible when the binary preserves the frame pointer. Optimizing compilers on x86-64 omit it unless `-fno-omit-frame-pointer` is passed, reusing the frame-pointer register as a general-purpose register, and many distribution builds inherit that default. **With the chain gone, perf has nothing to follow and every stack arrives one frame deep.** Two remedies exist:

1. Rebuild the target with `-fno-omit-frame-pointer`, where the source is under the profiler's control.
2. Unwind from debug information instead: `perf record -F 99 -a --call-graph dwarf -- sleep 30`. This reconstructs the stack from DWARF call frame information (CFI) rather than the frame-pointer chain. The costs are larger sample records — a portion of the stack is copied per sample, **8 KB by default, adjustable as `--call-graph dwarf,4096`** — and a perf binary built with an unwinding library (libunwind or elfutils' libdw).

**Permission denied.** Unprivileged `perf record` is gated by the `kernel.perf_event_paranoid` sysctl:

```bash
cat /proc/sys/kernel/perf_event_paranoid
```

**The value is a ceiling on what an unprivileged user may observe: lower numbers permit more.** At 2 — a common default — an unprivileged user may sample only processes they own, and kernel-space addresses are withheld, so system-wide `-a` recording fails and kernel frames are missing from the stacks that do come back. On a shared machine the narrower action is `sudo perf record ...` for the single run; `sudo sysctl kernel.perf_event_paranoid=-1` opens kernel-level profiling to every local user and is a hardening regression on a multi-tenant host.

## From perf.data to an SVG

The FlameGraph toolkit is cloned once:

```bash
git clone https://github.com/brendangregg/FlameGraph.git
```

The pipeline given by its README has three stages after capture:

```bash
perf record -F 99 -a -g -- sleep 60
perf script > out.perf
./stackcollapse-perf.pl out.perf > out.folded
./flamegraph.pl out.folded > out.svg
```

| Stage | Tool | Function |
|---|---|---|
| Capture | `perf record` | Samples stacks at 99 Hz for 60 s |
| Extract | `perf script` | Emits each sample as a textual stack trace |
| Fold | `stackcollapse-perf.pl` | Collapses each stack to one semicolon-separated `root;caller;leaf N` line, root first, with a trailing sample count |
| Render | `flamegraph.pl` | Converts the folded, counted stacks into an interactive SVG |

**The fold stage is where aggregation happens.** Identical stacks, however many times they were sampled, become one line plus a count; the renderer then needs only that count to size each box. `out.svg` is Scalable Vector Graphics with embedded JavaScript, so boxes respond to clicks (zoom into a subtree) and to hover (full stack and sample count).

## Reading the result

The two axes carry specific meanings, neither of which is the intuitive one.

- **Width is time, not order.** The x-axis is not chronological: stacks are sorted alphabetically. A box's width is proportional to how often that function appeared in a sample, counting itself and everything it called. Greater width means greater total on-CPU time.
- **The y-axis is stack depth.** Each box rests on its caller. The bottom row is the root — commonly `main` or a thread entry point. **The top box of any tower is the frame that was executing on the CPU when the timer fired; everything beneath it is ancestry.**

The shape to look for is a **wide plateau near the top of a tower**. A frame that is wide and flat-topped, with nothing or only thin slivers above it, indicates that the function itself rather than its callees holds the CPU. A tall narrow spike states the opposite: a deep call chain in which no single frame costs much. Because the whole profile is drawn at one proportional scale, the largest consumer is identified by area rather than by reading a sorted table.

A comparison is stronger evidence than a single graph. Running the same pipeline against `-p <PID>` for a service under real load, before and after a change, distinguishes a genuine fix — a plateau that narrows or disappears — from a change that relocates the same work into a different frame.

## Pitfalls

- Sampling with `-g` but without frame pointers yields one-frame stacks that still render: the flame graph is a row of unrelated leaf boxes with no towers, which reads as "no call graph" rather than as an error.
- Reading the x-axis as a timeline attributes ordering to alphabetical sort; two adjacent boxes have no temporal relationship whatsoever.
- Profiling a stripped binary produces `[unknown]` boxes that merge unrelated functions under one label, inflating an apparent hotspot that is an artifact of failed symbol resolution.
- `--call-graph dwarf` copies stack memory per sample, so a high sample rate or a long system-wide run inflates `perf.data` substantially compared with the frame-pointer walk.
- Sampling at exactly 100 Hz risks lockstep with periodic timer activity, biasing the sample toward work that happens on that beat; 99 Hz avoids the alignment.
- `kernel.perf_event_paranoid` at 2 causes system-wide `perf record -a` to fail for unprivileged users; the failure is a permissions error, not an empty profile.
- Interpreting small width differences as real: at 99 Hz over 60 seconds the sample count bounds resolution, and a few-percent difference between two frames is within sampling noise.
- A CPU flame graph shows on-CPU time only; a process blocked on input/output or on a lock contributes no samples and is invisible in the graph regardless of how long it waits.
