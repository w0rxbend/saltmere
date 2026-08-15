---
title: "async-profiler: JVM Flame Graphs Without Safepoint Bias"
date: 2026-08-15
track: scala-jvm
summary: "Most JVM samplers can only observe stacks at safepoints, so they systematically misattribute CPU time. async-profiler samples via perf_events and walks Java stacks asynchronously, producing mixed Java and native flame graphs from a one-line attach. Commands for CPU, alloc, lock and wall profiling, plus JFR output and the 4.x heatmaps."
reading_time: 6
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

**Gist.** A sampling profiler that collects Java stacks through the JVM Tool Interface (JVMTI) can only observe a thread when that thread has reached a **safepoint**, so time spent in regions containing no safepoint poll is attributed to whichever frame follows the region — an error that is systematic rather than random. async-profiler removes that constraint by taking samples from a signal handler driven by the Linux `perf_events` subsystem and unwinding the Java stack asynchronously, which places each sample at the instruction the processor was executing and yields mixed Java-plus-native stacks. The cost is that asynchronous unwinding runs outside the JVM's own consistency guarantees and depends on kernel permissions, so it needs its own stack walker and degrades to timer-based modes when `perf_events` access is denied.

The tool underlies both of the flame-graph workflows described elsewhere in this track: graphs built from `perf` output (see the perf-flame-graphs article) and continuous profiling with Pyroscope (see the pyroscope article). The current release line is **v4.5** (July 2026).

## Safepoint bias

A classic Java sampler — VisualVM, older JProfiler modes — ticks a timer and calls the JVMTI function `GetAllStackTraces`. That call executes only when every thread has stopped at a safepoint: one of the polling points the JIT compiler — which translates bytecode to machine code at runtime — emits at method returns and at back-edges of uncounted loops. Two distortions follow.

The first is misattribution. The sample does not record where a thread was when the timer fired; it records where the thread **next reached a safepoint**. Counted loops and aggressively inlined code contain no polls, so the time they consume is billed to the frame holding the next poll. The second is rate. Bringing all threads to a safepoint is expensive enough that the sampling frequency has to stay low, which widens the confidence interval on every measurement. Wakart's write-up demonstrates profilers naming the wrong hottest method on small benchmarks. The name for the combined effect is **safepoint bias**: the profiler observes only the places where the JVM permits observation, and those places are correlated with the code being measured.

async-profiler does not use JVMTI for sampling. It requests that `perf_events` interrupt the process every N cycles of consumed CPU time and, from inside the resulting signal handler, walks the Java stack asynchronously — historically through HotSpot's internal `AsyncGetCallTrace` entry point, and in the 4.x line through the project's own stack walker built on VMStructs, which replaces the dependency on `AsyncGetCallTrace`. **No safepoint is required, so each sample lands at the instruction the processor was executing.** Because `perf_events` also supplies the native and kernel portion of the stack, the resulting graph is **mixed-mode**: Scala frames, JVM C++ frames such as garbage-collection and JIT threads, libc frames and kernel frames appear in one picture.

### Implementation sketch (Scala)

The shape of code that safepoint-biased sampling mishandles is a counted loop whose body is inlined, containing no poll from entry to exit:

```scala
object HotLoop:
  // A counted loop over Int: HotSpot may emit no safepoint poll on the back-edge,
  // so a JVMTI sampler is unlikely to observe a thread while it is inside this method.
  def mix(data: Array[Long]): Long =
    var acc = 0L
    var i = 0
    while i < data.length do
      acc = acc * 6364136223846793005L + data(i)
      i += 1
    acc

  def report(data: Array[Long], rounds: Int): Long =
    var total = 0L
    var r = 0
    while r < rounds do
      total += mix(data)          // time spent here is billed to the next poll
      r += 1
    total
```

A JVMTI-based sampler tends to attribute the cycles of `mix` to a frame reached after the loop ends. A `perf_events` sample interrupts inside `mix` itself.

## Attaching

Since 3.0 the distribution includes a launcher binary, `asprof`, which attaches to a running JVM by process identifier, requiring neither a restart nor agent flags:

```bash
# 30 seconds of CPU profiling -> interactive flame graph
asprof -d 30 -f /tmp/cpu.html 8983

# or by name via jps, starting and stopping explicitly
asprof start MyApp
asprof stop -f /tmp/cpu.html MyApp
```

The default is CPU sampling at a **10 ms interval**; `-i` changes it. An `.html` output extension selects the built-in flame graph, a standalone file with search, zoom and, since 4.x, a dark-mode toggle. Kernel symbol access for `perf_events` may require `sysctl kernel.perf_event_paranoid=1` and `kernel.kptr_restrict=0`; without them async-profiler falls back to the `ctimer` and `itimer` modes, which remain free of safepoint bias but lose kernel frames.

## Four events, four questions

The `-e` flag determines what a sample denotes.

```bash
asprof -d 30 -e cpu   -f cpu.html   8983   # where do cycles go?
asprof -d 30 -e wall  -f wall.html  8983   # where does elapsed time go, blocking included?
asprof -d 30 -e alloc -f alloc.html 8983   # which stacks allocate?
asprof -d 30 -e lock  -f lock.html  8983   # which stacks contend?
```

**cpu** samples only threads that are on-CPU. **wall** samples every thread regardless of state, so a request blocked in `epoll_wait` or inside a JDBC call becomes visible; this is the mode for latency work, and `-t` splits the graph per thread. **alloc** does not sample time: it instruments thread-local allocation buffer (TLAB) slow paths, so each sample is a stack that allocated, weighted by bytes. **lock** records stacks that waited on contended monitors and on `ReentrantLock`, weighted by wait time. `--total` weights allocation graphs by total bytes. Release 4.0 added a **nativemem** profiler covering `malloc` paths outside the Java heap.

## JFR recordings and heatmaps

Flame-graph HTML is an aggregate and discards the time axis. For intermittent behaviour, record JDK Flight Recorder (JFR) output instead and capture several event types in one pass:

```bash
asprof -d 300 -e cpu --alloc 2m --lock 10ms -o jfr -f app.jfr 8983
jfrconv --cpu  app.jfr cpu.html        # slice back out per event type
jfrconv -o heatmap app.jfr heat.html
```

The result is a standard JFR file readable by JDK Mission Control, and the bundled `jfrconv` converts it into flame graphs or collapsed stacks. The **heatmap** converter, added in 4.0, is the reason to prefer this route: a two-dimensional timeline of coloured blocks, one block per short time slice, with intensity as a third dimension. The documentation describes long recordings remaining a single self-contained HTML file. Selecting a band produces the flame graph for that interval alone, which converts a periodic p99 spike into a locatable event.

## Reading a JVM graph

Flame-graph mechanics are covered in the perf article; what is JVM-specific is the colouring and the recurring shapes. Green frames are Java or Scala, yellow are JVM C++, red are other native code. A wide yellow tower rooted in the compiler-thread frames indicates the JIT compiler still working, which suggests profiling later or examining code-cache pressure. Garbage-collection worker towers dominating a *cpu* profile point at the *alloc* profile rather than at the algorithm. Wide park or `epoll` plateaus in a *wall* profile combined with a flat *cpu* profile indicate waiting rather than computing, which directs attention to the lock graph or to the downstream service.

## Pitfalls

- A profile taken with `-e cpu` on a service that is mostly blocked shows a nearly empty graph, because threads off-CPU are never sampled; the elapsed time lives in the *wall* profile.
- Kernel frames vanish and native symbols degrade when `perf_event_paranoid` or `kptr_restrict` deny access, because the tool silently falls back to `ctimer` or `itimer` sampling rather than failing.
- An *alloc* profile weighted by sample count over-represents frequent small allocations relative to few large ones; `--total` re-weights by bytes.
- Flame-graph HTML aggregates the entire recording, so a spike confined to a few seconds of a 300-second run is diluted below visibility; the JFR plus heatmap route preserves the time axis.
- A profile started immediately after deployment is dominated by JIT compilation and by interpreted frames, and does not describe steady-state behaviour.
- The *lock* event records contended monitor and `ReentrantLock` waits, so contention expressed through other mechanisms does not appear in that graph at all.
