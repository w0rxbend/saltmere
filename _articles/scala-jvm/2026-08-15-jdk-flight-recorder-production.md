---
title: "JDK Flight Recorder: always-on production profiling"
date: 2026-08-15
track: scala-jvm
summary: "JFR is the profiler that's already inside your JVM: a ring buffer of GC, lock, allocation, I/O, and method-sample events cheap enough to leave running in production forever, dumped on demand when something goes wrong. One flag turns it on, the jfr CLI reads the output, and JDK 25's JEPs 509/518/520 finally fix its weakest point — CPU profiling. Plus: when to reach for JFR versus async-profiler."
reading_time: 6
tags: [jfr, jvm, profiling, observability, jdk25, mission-control]
sources:
  - title: "JEP 349: JFR Event Streaming"
    url: "https://openjdk.org/jeps/349"
  - title: "JEP 509: JFR CPU-Time Profiling (Experimental)"
    url: "https://openjdk.org/jeps/509"
  - title: "JEP 518: JFR Cooperative Sampling"
    url: "https://openjdk.org/jeps/518"
  - title: "What's new for JFR in JDK 25 — Erik Gahlin"
    url: "https://egahlin.github.io/2025/05/31/whats-new-in-jdk-25.html"
  - title: "JDK Mission Control 9 — GA builds (jdk.java.net)"
    url: "https://jdk.java.net/jmc/9/"
---

The [async-profiler article](/articles/scala-jvm/2026-08-15-async-profiler-jvm-flamegraphs) covered the tool you *attach* when something is on fire. **JDK Flight Recorder** is the opposite philosophy: instrumentation that is *already running* when the fire starts. Born in JRockit, locked for years behind Oracle's commercial `-XX:+UnlockCommercialFeatures` flag, JFR was open-sourced into OpenJDK with **JDK 11** (JEP 328, 2018) — so every JVM you deploy today ships a full production profiler that most teams leave switched off.

## The flight-recorder model

JFR is named after the aircraft black box, and the analogy is exact. The JVM emits typed, timestamped **events** — GC pauses, safepoints, lock contention, thread parks, socket/file I/O, allocation samples, method samples, exceptions, compilation — into per-thread buffers that drain into a bounded on-disk repository. Old chunks are discarded; the recording never grows past its cap. Overhead with the `default` settings profile is targeted **below 1%**, which is the entire point: you don't decide *when* to profile, you decide when to *look*.

```bash
# always-on: bounded ring buffer, kept for 24h, dumped if the JVM exits
java -XX:StartFlightRecording=disk=true,maxsize=250M,maxage=1d,dumponexit=true,filename=/var/log/app/ \
     -jar app.jar

# later, when something went wrong in the last hour:
jcmd <pid> JFR.dump name=1 filename=incident.jfr
```

`jcmd <pid> JFR.start settings=profile duration=60s` starts a richer, still-cheap (~2%) recording on a live process with no restart. The crucial workflow difference from every attach-based tool: when a customer reports "it was slow at 14:32", the data from 14:32 **already exists**.

## Reading recordings

The `jfr` CLI in every JDK does more than people expect:

```bash
jfr summary incident.jfr                       # event counts by type
jfr print --events jdk.GCPhasePause incident.jfr
jfr view hot-methods incident.jfr              # JDK 21+: prebuilt reports
jfr view allocation-by-site incident.jfr
jfr scrub --exclude-events jdk.SystemProcess incident.jfr clean.jfr
```

For interactive analysis, **JDK Mission Control** — currently **JMC 9** (9.1.2 is the latest GA on jdk.java.net) — gives flame views, latency histograms, and an automated rule engine that flags things like undersized heaps. And since **JEP 349** (JDK 14) you don't have to wait for a dump at all: `RecordingStream` subscribes to events in-process, which is how you wire JFR into your own metrics.

```scala
import jdk.jfr.consumer.RecordingStream
val rs = new RecordingStream()
rs.enable("jdk.JavaMonitorEnter").withThreshold(java.time.Duration.ofMillis(10))
rs.onEvent("jdk.JavaMonitorEnter", e => log.warn(s"lock wait: ${e.getDuration}"))
rs.startAsync()
```

## Your events, not just the JVM's

Custom events are a class definition away, and since JFR is plain Java API it's identical from Scala:

```scala
import jdk.jfr.{Event, Label, Name}

@Name("com.saltmere.OrderProcessed") @Label("Order Processed")
class OrderEvent(@Label("Items") val items: Int) extends Event

val e = OrderEvent(cart.size)
e.begin(); processOrder(cart); e.commit()   // recorded only if enabled + over threshold
```

Events cost near-nothing when disabled, and they land on the same timeline as GC pauses and lock waits — so "this order took 900ms" sits directly above the safepoint that caused it. JDK 25 added `@Throttle` (cap an event at e.g. `"300/s"`) and `@Contextual` for tracing-style context propagation.

## JDK 25 fixed the weak spot

JFR's historical weakness was CPU profiling: its method sampler was safepoint-influenced and could miss or misattribute stacks. Three JEPs shipped in **JDK 25** (September 2025) attack exactly that:

- **JEP 518 (Cooperative Sampling)** reworks the sampler to walk stacks only at well-defined points, making sampling safer and more reliable (no more crashy asynchronous walks) without giving up accuracy of *where* the sample was taken.
- **JEP 509 (CPU-Time Profiling, experimental, Linux-only)** adds a timer-based `jdk.CPUTimeSample` event driven by actual consumed CPU time — the same signal async-profiler uses — closing most of the accuracy gap.
- **JEP 520 (Method Timing & Tracing)** adds exact (non-sampled) timing/tracing events for methods you name via filters — deterministic answers for "how often is this called and by whom", no bytecode-agent required.

Plus `report-on-exit`, which prints a hot-methods or GC report to stdout when the JVM exits — profiling for batch jobs with zero tooling.

## JFR or async-profiler?

| | JFR | async-profiler |
|---|---|---|
| Deployment | built into the JDK, always-on | attach agent / binary |
| Coverage | whole JVM: GC, locks, I/O, alloc, custom events | CPU, alloc, locks, wall |
| CPU profile fidelity | good since JDK 25 (JEP 509/518) | best-in-class, incl. native/kernel frames |
| Overhead | <1% default profile | low, but attach-when-needed |
| Output | .jfr → JMC / `jfr` CLI | flame graphs, JFR format, heatmaps |

The practical split: **run JFR always**, everywhere, as your black box and first responder — it answers "what happened at 14:32" across every subsystem. Reach for **async-profiler** when the question narrows to CPU cycles and you need native frames, kernel stacks, or perf-event counters. They even meet in the middle: async-profiler can emit JFR-format output, and JMC opens it.

**Try next:** add `-XX:StartFlightRecording=maxsize=100M,maxage=6h` to one production service today, wait a week for the first incident, then `jcmd <pid> JFR.dump` and open it with `jfr view hot-methods` — the argument for rolling it out fleet-wide makes itself.
