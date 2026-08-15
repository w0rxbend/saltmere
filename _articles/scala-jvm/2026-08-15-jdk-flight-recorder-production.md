---
title: "JDK Flight Recorder: always-on production profiling"
date: 2026-08-15
track: scala-jvm
summary: "JFR is the profiler already inside every JVM: a bounded ring of GC, lock, allocation, I/O and method-sample events cheap enough to leave running permanently, dumped on demand after an incident. One flag enables it, the jfr CLI reads the output, and JDK 25's JEPs 509/518/520 address its weakest point, CPU profiling. Includes the division of labour between JFR and async-profiler."
reading_time: 7
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

**Gist.** An attach-when-it-breaks profiler cannot answer questions about an incident that has already ended, because the process was not being observed while it happened. JDK Flight Recorder (JFR) inverts the model: the Java Virtual Machine (JVM) continuously emits typed, timestamped events into a bounded on-disk repository, so the data for 14:32 exists before anyone asks for it. The cost is a permanent overhead budget — **targeted below 1% with the `default` settings profile** — and a fixed retention window: once the size or age cap is reached, the oldest chunks are discarded whether or not they mattered.

The [async-profiler article](/articles/scala-jvm/2026-08-15-async-profiler-jvm-flamegraphs) covered the tool attached after the fact. JFR is instrumentation that is already running when the fault begins. It originated in JRockit, was gated for years behind Oracle's commercial `-XX:+UnlockCommercialFeatures` flag, and was open-sourced into OpenJDK in **JDK 11** (JEP 328, 2018). Every JVM deployed since then ships a production profiler, frequently left disabled.

## The recording model

The recorded unit is an **event**: a typed record with a start timestamp, optional duration, a thread, and named fields. The JVM defines events for garbage-collection (GC) pauses, safepoints, lock contention, thread parks, socket and file input/output, allocation samples, method samples, exceptions and dynamic (JIT) compilation.

The write path is the load-bearing part. Events are written into **per-thread buffers**, which avoids a shared lock on the hot path; full buffers drain into a global buffer and then into **chunks** in the repository. A recording is therefore a sequence of self-describing chunks rather than one monolithic file, which is what makes the retention policy cheap: enforcing `maxsize` or `maxage` is chunk deletion, not rewriting. **The recording never grows past its cap, and it never blocks the application to stay under it — it drops the oldest data instead.**

```bash
# always-on: bounded ring, kept for 24h, dumped if the JVM exits
java -XX:StartFlightRecording=name=always-on,disk=true,maxsize=250M,maxage=1d,\
dumponexit=true,filename=/var/log/app/exit.jfr \
     -jar app.jar

# later, when something went wrong in the last hour:
jcmd <pid> JFR.dump name=always-on filename=incident.jfr
```

`jcmd <pid> JFR.start settings=profile duration=60s` starts a richer recording — roughly **2% overhead** — on a live process without a restart. Two settings profiles ship with the JDK: `default` for continuous use, `profile` for bounded investigation.

Each event type carries an **enablement flag and a threshold**. A disabled event costs a flag test at the emission site; an enabled event below its threshold is emitted and then discarded at commit. This is why raising thresholds, rather than disabling event types, is the usual way to trade fidelity for overhead.

## Reading recordings

The `jfr` command in every JDK reads and transforms recordings without a graphical tool:

```bash
jfr summary incident.jfr                       # event counts by type
jfr print --events jdk.GCPhasePause incident.jfr
jfr view hot-methods incident.jfr              # JDK 21+: prebuilt reports
jfr view allocation-by-site incident.jfr
jfr scrub --exclude-events jdk.SystemProcess incident.jfr clean.jfr
```

`jfr scrub` matters for anything leaving the production boundary: recordings contain command lines, environment variables and system properties, which commonly carry credentials.

For interactive analysis, **JDK Mission Control** — **JMC 9**, distributed as general-availability builds on jdk.java.net — provides flame views, latency histograms and an automated rule engine that flags conditions such as an undersized heap.

**JEP 349** (JDK 14) removed the dump step for in-process consumers: `RecordingStream` delivers events to a callback as chunks are rotated, which is the supported path for feeding JFR data into an application's own metrics.

### Implementation sketch (Scala)

The JFR API is plain Java, so it is used unchanged from Scala. Consuming lock-contention events in-process:

```scala
import jdk.jfr.consumer.RecordingStream
import java.time.Duration

val rs = RecordingStream()

// enablement and threshold are per event type: below 10 ms the event is discarded at commit
rs.enable("jdk.JavaMonitorEnter").withThreshold(Duration.ofMillis(10))

rs.onEvent("jdk.JavaMonitorEnter", e =>
  log.warn(s"lock wait ${e.getDuration} on ${e.getClass("monitorClass").getName}")
)

rs.startAsync()   // callbacks run on the stream's own thread, not the emitting thread
```

Application-defined events are a class declaration:

```scala
import jdk.jfr.{Event, Label, Name}
import scala.annotation.meta.field

@Name("com.saltmere.OrderProcessed") @Label("Order Processed")
class OrderEvent(@(Label @field)("Items") val items: Int) extends Event

val e = OrderEvent(cart.size)
e.begin()
processOrder(cart)
e.commit()   // a commit below the event type's threshold discards the record
```

The value of a custom event is not the measurement — an existing timer supplies that — but **the shared timeline**: an order taking 900 ms is recorded against the same clock as the safepoint and the GC pause that overlap it, so attribution does not depend on correlating two systems' timestamps. Two annotations shape how custom events behave: `@Throttle` caps emission at a stated rate such as `"300/s"`, and `@Contextual` — added in JDK 25 — marks fields carrying tracing-style context.

## What JDK 25 changed in CPU profiling

JFR's method sampler was safepoint-influenced: samples were taken at points the JVM already had to reach, so stacks could be missed or misattributed relative to where the thread spent CPU time. Three JEPs in **JDK 25** address this.

- **JEP 518 (Cooperative Sampling)** reworks the sampler to walk stacks only at well-defined points, making the walk safer and more reliable than asynchronous walking, without losing accuracy about where the sample was taken.
- **JEP 509 (CPU-Time Profiling)** is **experimental and Linux-only**. It adds a `jdk.CPUTimeSample` event driven by a timer on consumed CPU time — the same signal async-profiler uses — rather than by wall clock.
- **JEP 520 (Method Timing & Tracing)** adds exact, non-sampled timing and tracing events for methods selected by filter, giving deterministic call counts and callers without a bytecode-instrumenting agent.

JDK 25 also adds `report-on-exit`, which prints a report such as hot methods or GC to standard output at JVM exit — usable for batch jobs with no analysis tooling in the environment.

## JFR or async-profiler

| | JFR | async-profiler |
|---|---|---|
| Deployment | built into the JDK, always-on | attach agent or binary |
| Coverage | whole JVM: GC, locks, I/O, allocation, custom events | CPU, allocation, locks, wall clock |
| CPU profile fidelity | improved in JDK 25 (JEP 509/518) | includes native and kernel frames |
| Overhead | below 1% with `default` | low, but attached when needed |
| Output | `.jfr` → JMC or `jfr` CLI | flame graphs, JFR format, heatmaps |

The division of labour follows from the deployment model. JFR runs permanently and answers cross-subsystem questions about a past interval. async-profiler is the instrument once the question narrows to CPU cycles and requires native frames, kernel stacks or perf-event counters. The two are not exclusive: async-profiler can emit JFR-format output, which JMC opens.

## Pitfalls

- **`maxage` and `maxsize` are both caps, and the tighter one wins.** A recording configured for 24 hours but capped at 100 MB on a chatty service retains far less than a day, and the shortfall is silent — the oldest chunks are already gone when the dump is taken.
- **Without `dumponexit=true`, a `disk=true` recording leaves the repository behind but no dump.** A JVM killed by the out-of-memory killer produces no incident file unless the repository directory itself is collected.
- **A recording dumped without `jfr scrub` carries the process command line, environment variables and system properties.** Any credential passed as a JVM argument or environment variable travels with the file.
- **Raising fidelity by switching to `settings=profile` on every instance turns a sub-1% budget into roughly 2% fleet-wide.** The profile setting is intended for bounded investigation, not continuous operation.
- **JEP 509 CPU-time sampling is experimental and Linux-only.** A configuration relying on `jdk.CPUTimeSample` yields nothing on macOS or Windows, and experimental features require the corresponding unlock flag.
- **An event type that is enabled but has a threshold above the durations of interest records nothing.** The absence of `jdk.JavaMonitorEnter` events in a recording is evidence about the threshold as much as about contention.
- **`RecordingStream` callbacks run on the stream's thread.** Blocking work inside a handler delays consumption of subsequent events rather than the application's own threads, so the loss appears as missing observations, not as latency.
