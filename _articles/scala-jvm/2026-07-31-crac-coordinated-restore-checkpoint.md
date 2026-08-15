---
title: "CRaC: Restoring a Warmed-Up JVM in Milliseconds"
date: 2026-07-31
track: scala-jvm
summary: "Coordinated Restore at Checkpoint snapshots a running, JIT-warmed JVM to disk and restores it in tens of milliseconds, avoiding both the cold-start ramp and an ahead-of-time rebuild. The cost: open files and sockets abort the checkpoint, so resources must be closed in beforeCheckpoint() and reopened in afterRestore()."
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

**Gist.** A freshly launched Java Virtual Machine (JVM) interprets bytecode before the JIT compiler — which compiles methods to machine code at run time — recompiles the hot paths, so a service is slowest in its first seconds — precisely the window that scale-to-zero and per-request billing charge for. Coordinated Restore at Checkpoint (CRaC), an OpenJDK project, removes that window by snapshotting an already-warmed process to disk and restoring the same process later. The cost is that the snapshot is only valid if the process holds no live external state at checkpoint time: any disallowed open file descriptor or socket aborts the checkpoint, so applications must implement explicit teardown and re-establishment hooks.

## Snapshotting a process, warmth included

On Linux, CRaC uses **CRIU** (Checkpoint/Restore In Userspace) to write the process — memory, threads, open file descriptors — into an image directory. Restore does not produce a fresh JVM that must warm up again; it produces **the same HotSpot process, with its heap contents and JIT-compiled code intact**. That is the structural difference from GraalVM native image, which also starts quickly but yields an ahead-of-time-compiled binary with different peak-performance and compatibility characteristics. CRaC keeps a conventional, warmed JVM.

Azul reports a Spring Boot application reaching first operation in roughly **4 seconds** on a normal start and about **40 milliseconds** on CRaC restore — roughly two orders of magnitude, because the warmup happened before the checkpoint and was frozen into the image.

Three commands cover the whole cycle:

```bash
# 1. Run with checkpointing enabled (image dir must exist and be empty)
java -XX:CRaCCheckpointTo=$HOME/crac-image -jar my_app.jar

# 2. Once warmed up, trigger the checkpoint (the JVM exits after writing the image)
jcmd my_app.jar JDK.checkpoint

# 3. Later, restore the warmed JVM
java -XX:CRaCRestoreFrom=$HOME/crac-image
```

A CRaC-enabled JDK is required: the OpenJDK project publishes reference builds, and Azul Zulu and BellSoft Liberica ship CRaC-enabled distributions. Application code compiles against the small portable `org.crac` shim (`io.github.crac:org-crac` on Maven Central), which delegates to the real implementation when one is present and **degrades to a no-op on a stock JDK** — so a single artifact runs on both.

## The invariant: no live external state at checkpoint

A live TCP connection cannot be meaningfully frozen and resumed in a different process, on a different host, minutes later: the peer has its own view of the connection and no obligation to preserve it. CRaC therefore enforces the invariant rather than papering over it. **If any disallowed file descriptor or socket is open when `JDK.checkpoint` runs, the checkpoint aborts** and the offending descriptors are printed to the application's console.

The diagnostic detail that costs the most time: `jcmd` reports `Command executed successfully` regardless, because it only confirms the diagnostic command was delivered. **The actual abort message appears in the application's own stdout**, not in the `jcmd` output.

The mechanism for satisfying the invariant is the **`Resource` lifecycle** in `org.crac`. An object implements `Resource`, registers itself with a `Context`, and receives two callbacks: `beforeCheckpoint`, which must release everything the checkpoint forbids, and `afterRestore`, which re-establishes it.

The ordering is the part that matters for correctness. On the global context, **`beforeCheckpoint` callbacks run in reverse registration order and `afterRestore` in forward order** — the standard teardown/bring-up discipline, so a resource registered after its dependency is torn down before that dependency and restored after it.

The second subtlety is reference strength. **The `Context` holds only a `WeakReference` to each registered `Resource`.** A `Resource` whose only reachability path was the registration itself can be collected, and its hooks are then silently skipped: the checkpoint aborts on a descriptor nobody closed, or, worse, restores with a connection object that was never refreshed. Registration must therefore be paired with a strong reference held by the application.

`afterRestore` is also the only correct place to refresh anything whose validity is bound to wall-clock time or to process identity: pseudo-random number generator seeds duplicated across every restore of the same image, authentication tokens that may have expired while the image sat on disk, cached timestamps.

### Implementation sketch (Scala)

```scala
import org.crac.{Context, Core, Resource}

final class PooledClient(connect: () => Session) extends Resource:
  @volatile private var session: Option[Session] = Some(connect())

  // The Context holds only a WeakReference, so the caller must retain
  // this instance for the hooks to fire at all.
  Core.getGlobalContext.register(this)

  def beforeCheckpoint(ctx: Context[? <: Resource]): Unit =
    session.foreach(_.close())   // any surviving socket aborts the checkpoint
    session = None

  def afterRestore(ctx: Context[? <: Resource]): Unit =
    session = Some(connect())    // also re-seed RNGs and re-fetch expiring tokens here

  def current: Session =
    session.getOrElse(throw IllegalStateException("checkpointed"))

object App:
  // Strong reference: a field, not a local that goes out of scope.
  private val client = PooledClient(() => Session.open())
```

The `IllegalStateException` branch is load-bearing rather than defensive: between `beforeCheckpoint` and `afterRestore` the process is still running, and **any thread that was not quiesced can observe the closed state**.

## The image is a raw memory dump

Because the image is a byte-level dump of process memory, **every credential, key and session token resident in the heap at checkpoint time is written verbatim into the image files**. Checkpoint images are therefore artifacts with the same sensitivity as the secrets the process held: access to the image directory is equivalent to access to those secrets. Refreshing secrets in `afterRestore` limits how long a leaked image stays useful, but does not remove what was already serialised.

## Existing integrations

**Spring Boot 3.2 and later** builds on the same API. It can checkpoint automatically once the application context is refreshed — `-Dspring.context.checkpoint=onRefresh` combined with `-XX:CRaCCheckpointTo=...` — or on demand via `jcmd <pid> JDK.checkpoint`, stopping and restarting lifecycle beans around the snapshot.

**AWS Lambda SnapStart** snapshots a Firecracker microVM rather than using CRIU, but exposes the same `org.crac` `Resource` contract for Java. The `beforeCheckpoint`/`afterRestore` implementations written for CRaC are the same ones that apply to Lambda cold starts.

## Pitfalls

- **The checkpoint aborts and `jcmd` still reports success.** `jcmd` confirms only that the diagnostic command was delivered; the descriptor-level abort message is written to the application's stdout.
- **A `Resource`'s hooks never fire.** The `Context` registration is weak, so an instance with no other strong reference is collected and skipped without any error.
- **Restored processes produce identical "random" values.** A pseudo-random number generator seeded before the checkpoint has its state captured in the image and replays it on every restore unless reseeded in `afterRestore`.
- **Requests fail immediately after restore with expired credentials.** Tokens fetched before the checkpoint keep their original expiry while the image sits on disk; only `afterRestore` can re-fetch them.
- **Secrets leak through the image directory.** The image is a raw memory dump, so heap-resident keys are present in plaintext in the files.
- **Dependent resources are torn down or restored in the wrong order.** Ordering follows registration order — reverse for `beforeCheckpoint`, forward for `afterRestore` — so a dependency registered after its dependent inverts the intended sequence.
- **The checkpoint fails before it starts.** `-XX:CRaCCheckpointTo` requires the target directory to exist and to be empty.
