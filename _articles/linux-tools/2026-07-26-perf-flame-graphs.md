---
title: "Flame graphs: see where your CPU time actually goes"
date: 2026-07-26
track: linux-tools
summary: "perf record samples a running program's stack hundreds of times a second; a flame graph turns those thousands of samples into one picture where the widest boxes are your hottest code. Here's the full record-to-SVG pipeline, and how to fix it when the stacks come back broken."
reading_time: 5
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

`strace` and bpftrace answer "what is this process doing." Neither answers "which function is eating my CPU." For that you want a statistical profiler: `perf record` interrupts the CPU at a fixed frequency, grabs the current stack trace, and moves on. Do that a few thousand times and the sample counts *are* the profile — no instrumentation, no per-call overhead, just arithmetic on where the program happened to be standing when the timer fired.

## Sampling vs tracing

This distinction is why perf is cheap enough to run in production. A tracer (like `strace`, or a uprobe on every function entry) intercepts *every* occurrence of an event — precise, but overhead scales with how often the event fires. A sampling profiler like `perf record` instead uses `perf_events` to fire a timer at, say, 99 times a second, and only looks at the stack on those interrupts. Overhead is bounded by the sample rate, not by program behavior — exactly the trade you want for CPU profiling, since you care about the *statistical distribution* of where time goes, not every call. Sample at 99 Hz for 60 seconds and a function using 40% of CPU time shows up in roughly 40% of ~5,900 samples: plenty of resolution for finding hotspots, at overhead low enough to run on a live service.

## The core command

```bash
perf record -F 99 -a -g -- sleep 30
```

- `-F 99` — sample at 99 Hz. Not 100: an exact round number risks lockstep with periodic kernel/application activity (timers, schedulers) and skewing the sample toward whatever happens on that beat.
- `-a` — profile all CPUs system-wide, not just one command.
- `-g` — record the call graph (stack trace) at each sample, not just the leaf instruction pointer. Without this you get a flat profile of whichever function happened to be running, with no idea who called it.
- `-- sleep 30` — a trick, not a target: `sleep` does nothing, so this just bounds the recording to 30 seconds of system-wide sampling. Swap it for `-p PID` to target one process instead of `-a`.

This writes `perf.data` in the current directory. Then:

```bash
perf report
```

`perf report` opens an interactive, `top`-like ncurses view sorted by overhead percentage, with call graphs you can expand per-function. It's the right first look — but for a busy multi-threaded service with deep stacks, scrolling a text tree of hundreds of call paths gets old fast. That's the problem flame graphs solve.

## When stacks come back broken

Two failure modes show up almost immediately and both look the same: `perf report` full of `[unknown]` frames or truncated one-entry stacks.

**Missing symbols.** perf resolves addresses to function names using the binary's symbol table. Stripped binaries, JIT-compiled code, and optimized builds without debug info all produce `[unknown]`. Fix: build or install the `-dbg`/`-dbgsym` package for the binary, or use `debuginfod` if your distro serves it, so perf can find symbols out of band.

**Missing/broken stacks — the frame pointer problem.** By default `-g` walks the stack using frame pointers (`--call-graph fp`). That only works if the binary was compiled with frame pointers preserved. Most distro packages and anything built with `-O2` are compiled with `-fomit-frame-pointer`, which reuses the frame-pointer register for something else — so `perf` has nothing to walk, and stacks come back one frame deep. Two fixes:

1. Rebuild the target with `-fno-omit-frame-pointer` if you control the source.
2. Or tell perf to unwind using debug info instead: `perf record -F 99 -a --call-graph dwarf -- sleep 30`. This walks the stack using DWARF CFI (call frame information) rather than the frame-pointer chain, at the cost of larger sample records (it snapshots part of the stack per sample — default 8 KB, tunable with `--call-graph dwarf,4096`) and needing perf built with libunwind support.

**Permission denied.** Unprivileged `perf record` is gated by `kernel.perf_event_paranoid`. Check it:

```bash
cat /proc/sys/kernel/perf_event_paranoid
```

A value of 2 or higher blocks unprivileged CPU-event sampling entirely. The fastest fix on a shared box is `sudo perf record ...` for that one run rather than permanently lowering the sysctl system-wide — `sudo sysctl kernel.perf_event_paranoid=-1` opens kernel-level profiling to every local user, which is a real hardening regression on a multi-tenant machine.

## From perf.data to a flame graph

Clone Brendan Gregg's FlameGraph toolkit once:

```bash
git clone https://github.com/brendangregg/FlameGraph.git
```

Then the three-stage pipeline, straight from its README:

```bash
perf record -F 99 -a -g -- sleep 60
perf script > out.perf
./stackcollapse-perf.pl out.perf > out.folded
./flamegraph.pl out.folded > out.svg
```

| Stage | Tool | What it does |
|---|---|---|
| Capture | `perf record` | Samples stacks at 99 Hz for 60s |
| Extract | `perf script` | Dumps each sample as a readable stack trace |
| Fold | `stackcollapse-perf.pl` | Collapses each stack into one `func;caller;caller N` line, with a trailing sample count |
| Render | `flamegraph.pl` | Turns the folded, counted stacks into an interactive SVG |

Open `out.svg` in a browser — it's real SVG with embedded JS, so boxes are clickable (zoom into a subtree) and hoverable (shows the full stack and sample count).

## Reading the flame graph

Two axes, and they mean specific, non-obvious things:

- **Width = time.** The x-axis is *not* chronological order — stacks are sorted alphabetically, not by when they occurred. A box's width is proportional to how often that function appeared in a sample, summed across itself and everything it called. Wider means more total on-CPU time.
- **Y-axis = stack depth.** Each box sits on top of its caller. The bottom row is the root (often `main` or a thread entry point); the top box in any given tower is the function that was actually executing on the CPU at sample time — everything below it is just ancestry.

What to look for: **wide plateaus**, especially near the top of a tower. A single frame that's wide *and* flat-topped (nothing above it, or only thin slivers) means that function itself — not something it calls — is where the CPU sits. A tall, narrow spike is the opposite story: deep call chain, little individual cost anywhere in it. Because everything is on one screen at a proportional scale, the worst offender is visually obvious without reading a sorted table — you're looking for the widest boxes, full stop.

**Try next:** run the same pipeline against `-p <PID>` for a live service under real load instead of `-a` system-wide, and compare the flame graph before and after an optimization — a genuine fix should visibly narrow or remove a plateau, not just move it.
