---
title: "async-profiler: Honest JVM Flame Graphs Without Safepoint Bias"
date: 2026-08-15
track: scala-jvm
summary: "Most JVM samplers can only see stacks at safepoints, so they systematically lie about where CPU time goes. async-profiler samples via perf_events and walks Java stacks asynchronously, giving mixed Java+native flame graphs from a one-line attach. Commands for CPU, alloc, lock, and wall profiling — plus JFR output and the 4.x heatmaps."
reading_time: 5
tags: [async-profiler, jvm, profiling, flame-graphs, performance, perf]
sources:
  - title: "async-profiler — GitHub repository and docs"
    url: "https://github.com/async-profiler/async-profiler"
  - title: "async-profiler — GitHub releases (v4.5, July 2026)"
    url: "https://github.com/async-profiler/async-profiler/releases"
  - title: "async-profiler docs — Heatmap.md"
    url: "https://github.com/async-profiler/async-profiler/blob/master/docs/Heatmap.md"
  - title: "Nitsan Wakart — Why (Most) Sampling Java Profilers Are Fucking Terrible"
    url: "https://psy-lob-saw.blogspot.com/2016/02/why-most-sampling-java-profilers-are.html"
  - title: "Baeldung — A Guide to async-profiler"
    url: "https://www.baeldung.com/java-async-profiler"
---

We have built flame graphs from `perf` output (see the perf-flame-graphs article) and shipped them continuously with Pyroscope (see the pyroscope article). On the JVM, the engine underneath both workflows is usually the same tool: **async-profiler**, currently at **v4.5** (July 2026). It exists because the obvious way to sample a JVM produces graphs that are not just noisy but *systematically wrong*.

## The safepoint lie

A classic Java sampler (VisualVM, old JProfiler modes) ticks a timer and calls JVMTI `GetAllStackTraces`. That call can only run when every thread is at a **safepoint** — one of the polling points the JIT emits at method returns and uncounted-loop back-edges. Two distortions follow. First, your sample doesn't capture where the thread *was* when the timer fired, but where it *next reached a safepoint* — hot counted loops and aggressively inlined code contain no polls, so their time gets billed to whatever safepoint comes after them. Second, stopping the world to sample costs enough that you sample rarely. Nitsan Wakart's write-up shows profilers confidently naming the wrong hottest method on trivial benchmarks. This is *safepoint bias*: the profiler can only see the places the JVM lets it look.

async-profiler sidesteps JVMTI entirely. It asks Linux **perf_events** to interrupt the process every N cycles of *actual CPU time*, and from the signal handler walks the Java stack asynchronously — historically via HotSpot's internal `AsyncGetCallTrace` call, and in the 4.x line via its own VMStructs-based stack walker that fixes AGCT's unwinding failure modes. No safepoint required, so samples land exactly where the CPU was. Because perf_events also provides the native and kernel stack, you get **mixed-mode** graphs: your Scala code, the JVM's C++ (GC, JIT threads), libc, and kernel frames in one picture.

## Attach in one line

Since 3.0 the tool ships a launcher binary, `asprof`, that attaches to a running JVM by PID — no restart, no agent flags:

```bash
# 30 seconds of CPU profiling -> interactive flame graph
asprof -d 30 -f /tmp/cpu.html 8983

# or by name via jps, and start/stop by hand
asprof start MyApp
asprof stop -f /tmp/cpu.html MyApp
```

Default is CPU sampling at 100 Hz per core (`-i` changes the interval). The `.html` extension selects the built-in d3-free flame graph — a standalone file with search, zoom, and (since 4.x) a dark-mode toggle. The one prerequisite worth knowing: kernel symbol access for perf_events may need `sysctl kernel.perf_event_paranoid=1` and `kernel.kptr_restrict=0`, otherwise async-profiler falls back to `ctimer`/`itimer` modes that still avoid safepoint bias but lose kernel frames.

## Four questions, four events

The `-e` flag switches what a sample *means*, and each mode answers a different production question:

```bash
asprof -d 30 -e cpu   -f cpu.html   8983   # where do cycles go?
asprof -d 30 -e wall  -f wall.html  8983   # where does time go, incl. blocking?
asprof -d 30 -e alloc -f alloc.html 8983   # who allocates?
asprof -d 30 -e lock  -f lock.html  8983   # who contends?
```

**cpu** samples only threads that are on-CPU. **wall** samples every thread regardless of state, so a request stuck in `epoll_wait` or a JDBC call finally shows up — this is the mode for latency work, and it pairs with `-t` to split the graph per thread. **alloc** doesn't sample time at all: it instruments TLAB slow paths, so each sample is a stack that allocated, weighted by bytes — cheap enough for production and the fastest way to find the code feeding your GC. **lock** records stacks that waited on contended monitors and `ReentrantLock`s, weighted by wait time. Add `--total` to weight alloc graphs by total bytes, and note that 4.0 also added a **nativemem** leak profiler for `malloc` paths outside the heap.

## JFR recordings and heatmaps

Flame graph HTML is an aggregate — it throws away *when*. For anything intermittent, record JFR instead, and capture several event types in one pass:

```bash
asprof -d 300 -e cpu --alloc 2m --lock 10ms -o jfr -f app.jfr 8983
jfrconv --cpu  app.jfr cpu.html        # slice back out per event type
jfrconv --cpu -o heatmap app.jfr heat.html
```

The output is a standard JFR file, readable by JDK Mission Control, and the bundled `jfrconv` converts it to flame graphs or collapsed stacks. The **heatmap** converter (new in 4.0) is the reason to prefer this route: a two-dimensional timeline of colored blocks, one per ~20 ms slice, with intensity as the third dimension — the docs quote handling 24-hour recordings at that granularity in a single self-contained HTML file. Select a hot band and you get the flame graph for exactly that interval, which turns "p99 spikes every few minutes" from a mystery into a click. The rest of the 4.x line has pushed the same production angle: 4.3 added native lock profiling, latency filtering, and Prometheus/JMX remote control; 4.4 added differential flame graphs; 4.5 added a span API for latency profiling and compatibility with the OpenTelemetry Profiles alpha.

## Reading a JVM graph

Flame graph mechanics are covered in the perf article; what is JVM-specific is the coloring and the usual suspects. Green frames are Java/Scala, yellow are JVM C++, red are other native code. A wide yellow tower under `Compile::Compile` is the JIT still warming up — profile later or check code cache pressure. GC worker towers that dominate a *cpu* profile mean the fix is in your *alloc* profile, not your algorithm. Wide `ThreadPark`/`epoll` plateaus in a *wall* profile with a flat *cpu* profile mean you are waiting, not computing — go look at the lock graph or the downstream service. The tool's job is to make the JVM stop hiding; the reading is the same skepticism you would apply to any perf data.

**Try next:** attach `asprof -d 60 -e wall -t -o jfr` to a service under load, convert the same recording with `jfrconv --cpu` and `--wall`, and diff the two graphs — every frame that grows in wall mode is blocking you can go hunt.
