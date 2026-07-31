---
title: "CRaC: Restoring a Warmed-Up JVM in Milliseconds"
date: 2026-07-31
track: scala-jvm
summary: "Coordinated Restore at Checkpoint snapshots a running, JIT-warmed JVM to disk and restores it in tens of milliseconds — no cold-start ramp, no native-image rebuild. The catch: open files and sockets abort the checkpoint, so you close them in beforeCheckpoint() and reopen in afterRestore()."
reading_time: 6
tags: [jvm, crac, startup, checkpoint-restore, spring-boot, criu]
sources:
  - title: "OpenJDK: Coordinated Restore at Checkpoint (project page)"
    url: "https://openjdk.org/projects/crac/"
  - title: "CRaC/docs — commands, concepts, and the Resource API"
    url: "https://github.com/CRaC/docs"
  - title: "Superfast Application Startup: Java on CRaC — Azul blog"
    url: "https://www.azul.com/blog/superfast-application-startup-java-on-crac/"
  - title: "Checkpoint and Restore With the JVM — Spring Boot reference"
    url: "https://docs.spring.io/spring-boot/reference/packaging/checkpoint-restore.html"
  - title: "Lambda SnapStart runtime hooks for Java (CRaC org.crac API) — AWS"
    url: "https://docs.aws.amazon.com/lambda/latest/dg/snapstart-runtime-hooks-java.html"
---

The JVM's dirty secret is that it's slow exactly when you most need it to be fast. A freshly launched service spends its first seconds interpreting bytecode, then re-JIT-compiling hot paths it has compiled a thousand times before on other machines. Scale-to-zero and per-request billing turn that warmup into real money and real tail latency. **CRaC** — Coordinated Restore at Checkpoint, an OpenJDK project — attacks it directly: snapshot a running, already-warmed JVM to disk, then restore that exact process later in tens of milliseconds.

## Snapshot the whole process, warmth included

On Linux, CRaC uses **CRIU** underneath to checkpoint the process — memory, threads, open file descriptors — to an image directory. What comes back on restore isn't a fresh JVM that has to warm up; it's the *same* HotSpot process with its heap and JIT-compiled code intact. That's the difference from GraalVM native image, which also starts fast but gives you an ahead-of-time-compiled binary with different peak-performance and compatibility characteristics. CRaC keeps a real, warmed JVM.

The numbers are the selling point. Azul reports a Spring Boot app going from roughly **4 seconds** to first operation on a normal start down to about **40 milliseconds** on CRaC restore — two orders of magnitude, because the warmup simply already happened and got frozen into the image.

The commands are three:

```bash
# 1. Run with checkpointing enabled (image dir must exist and be empty)
java -XX:CRaCCheckpointTo=$HOME/crac-image -jar my_app.jar

# 2. Once it's warmed up, trigger the checkpoint (the JVM exits after writing the image)
jcmd my_app.jar JDK.checkpoint

# 3. Later, restore the warmed JVM
java -XX:CRaCRestoreFrom=$HOME/crac-image
```

You need a CRaC-enabled JDK to do this. Azul Zulu ships commercial CRaC builds (it was first to GA, back in 2023) and the OpenJDK project publishes reference builds for JDK 17 and 24; BellSoft Liberica ships them too. Your application compiles against the small portable `org.crac` shim (`org.crac:crac` on Maven), which delegates to the real API when present and is a harmless no-op on a stock JDK — so the same jar runs everywhere.

## The catch: open resources abort the checkpoint

You cannot meaningfully freeze a live TCP socket or an open file and expect it to work in a different process, on a different host, minutes later. So CRaC refuses: if any disallowed file descriptors or sockets are open when you call `JDK.checkpoint`, the checkpoint **aborts** and prints the offending descriptors to the application's console. (Note `jcmd` itself always says "Command executed successfully" — the real error is in the app's own stdout.)

The fix is the **`Resource` lifecycle**. Implement `org.crac.Resource`, register it, and close your resources before the snapshot and reopen them after:

```scala
import org.crac.{Context, Core, Resource}

class CacheConnection extends Resource:
  // keep a strong reference — the Context holds only a WeakReference
  Core.getGlobalContext.register(this)

  def beforeCheckpoint(ctx: Context[? <: Resource]): Unit =
    close()        // drain pools, close sockets/files — or the checkpoint aborts

  def afterRestore(ctx: Context[? <: Resource]): Unit =
    reconnect()    // reopen connections; refresh clocks, tokens, RNG seeds

  private def close(): Unit = ???
  private def reconnect(): Unit = ???
```

`beforeCheckpoint` callbacks run in reverse registration order (tear down), `afterRestore` in forward order (bring back up). Two things to internalize. First, register a *strong* reference — the context only weakly references you, so a GC'd Resource silently skips its hooks. Second, `afterRestore` is where you refresh anything time- or secret-sensitive: reseed `Random`, re-fetch tokens that may have expired while the image sat on disk.

Which leads to the security caveat: **the image is a raw memory dump.** Any credentials, keys, or connection state resident in the heap at checkpoint time are written verbatim into the file. Treat checkpoint images as sensitive artifacts — encrypt them, lock down access, and refresh secrets on restore.

## You may already have hooks for this

**Spring Boot 3.2+** builds on the same API. It can checkpoint automatically once the context is refreshed (`-Dspring.context.checkpoint=onRefresh` together with `-XX:CRaCCheckpointTo=...`) or on demand via `jcmd <pid> JDK.checkpoint`, stopping and restarting lifecycle beans around the snapshot for you. And **AWS Lambda SnapStart**, though it snapshots a Firecracker microVM rather than using CRIU, exposes the *same* `org.crac` `Resource` contract for Java — so the `beforeCheckpoint`/`afterRestore` code you write for CRaC is exactly what tames Lambda cold starts.

**Try next:** Grab a Zulu CRaC JDK, take any Scala/Java HTTP service with a database pool, and run the three commands above without a `Resource` — watch the checkpoint abort and name your open socket. Then add the `Resource` that closes the pool in `beforeCheckpoint` and reopens it in `afterRestore`, checkpoint successfully, and time the restore against a normal start.
