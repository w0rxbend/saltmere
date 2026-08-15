---
title: "JDK 26 in Practice: HTTP/3, Primitive Patterns, and the Road Through 27 to Valhalla"
date: 2026-08-13
track: scala-jvm
summary: "JDK 26 shipped on 17 March 2026 with ten JEPs; the September 2026 slot belongs to JDK 27, now in rampdown. This article examines the three changes with the largest effect on a telemetry backend: HTTP/3 in HttpClient (JEP 517), primitive types in patterns (JEP 530), and the final-field mutation warnings of JEP 500."
reading_time: 6
tags: [java, jdk-26, http3, pattern-matching, valhalla]
sources:
  - title: "JDK 26 — OpenJDK project page"
    url: "https://openjdk.org/projects/jdk/26/"
  - title: "JDK 27 — OpenJDK project page"
    url: "https://openjdk.org/projects/jdk/27/"
  - title: "JDK 26: The new features in Java 26 — InfoWorld"
    url: "https://www.infoworld.com/article/4050993/jdk-26-the-new-features-in-java-26.html"
  - title: "Java 26 Features (with Examples) — HappyCoders"
    url: "https://www.happycoders.eu/java/java-26-features/"
  - title: "Project Valhalla's First Preview: JEP 401 Redefines == for Java Objects — InfoQ"
    url: "https://www.infoq.com/news/2026/08/jep401-value-objects-preview/"
---

**Gist.** JDK 26 is the current release as of August 2026 — it shipped on **17 March 2026** with ten JDK Enhancement Proposals (JEPs), and the September 2026 slot belongs to JDK 27, which entered rampdown in June. Three of the ten change how a service is written or run: HTTP/3 in the standard `HttpClient` (JEP 517), primitive types in patterns (JEP 530), and warnings for reflective mutation of `final` fields (JEP 500). Each carries a cost: HTTP/3 introduces a negotiation-dependent transport whose behaviour differs per peer, primitive patterns remain a preview feature that requires `--enable-preview` and can change, and JEP 500 places a deprecation clock on every library that rewrites final fields.

## The JDK 26 scorecard

The ten JEPs are: **500** (final-field mutation warnings), **504** (Applet API removed), **516** (ahead-of-time object caching with any garbage collector), **517** (HTTP/3 for `HttpClient`), **522** (G1 throughput via reduced synchronization), **524** (PEM encodings, second preview), **525** (structured concurrency, sixth preview), **526** (lazy constants, second preview), **529** (Vector API, eleventh incubation), and **530** (primitive types in patterns, fourth preview).

Structured concurrency, lazy constants (formerly stable values) and the Vector API were covered in the JDK 25 write-ups and re-preview or re-incubate in 26 rather than changing shape. The three sections below cover the remainder that alter production code.

## HTTP/3 in HttpClient (JEP 517)

The standard `HttpClient` can negotiate HTTP/3, which runs over QUIC rather than TCP. Opting in is a builder call on the client, the request, or both:

```java
HttpClient client = HttpClient.newBuilder()
    .version(HttpClient.Version.HTTP_3)
    .build();

HttpRequest req = HttpRequest.newBuilder(URI.create("https://ingest.example.com/v1/readings"))
    .version(HttpClient.Version.HTTP_3)
    .build();

HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
System.out.println(resp.version());   // HTTP_3 if negotiated
```

The version request is a preference, not a guarantee. **When the server does not offer HTTP/3, the exchange can complete over an earlier protocol version**, so enabling it is observable rather than fatal — the actual protocol used is reported by `resp.version()`, and that is the only reliable way to confirm negotiation succeeded.

The mechanism that matters is where multiplexing lives. HTTP/2 multiplexes concurrent streams over a single TCP connection, and TCP delivers bytes in order: **a single lost segment stalls delivery for every stream sharing that connection until retransmission completes**, the failure mode known as transport-level head-of-line blocking. QUIC carries streams over UDP and tracks per-stream delivery, so **a lost packet stalls only the stream whose data it carried**. The consequence is distributional: a workload of many small concurrent requests sees the difference in tail latency, not in the median, because head-of-line stalls are a loss-triggered event rather than a steady-state cost.

## Primitive types in patterns (JEP 530, fourth preview)

Pattern matching now admits `int`, `long`, `float` and the remaining primitive types in `instanceof` and `switch`. The load-bearing semantic is the match condition: **a primitive type pattern matches only when the conversion is lossless**. A value that would be truncated does not match; it falls through to the next case.

```java
// --enable-preview
static String classify(Object reading) {
    return switch (reading) {
        case int ppm when ppm > 1500 -> "co2-ventilate";
        case int ppm                 -> "co2:" + ppm;
        case float pm                -> "pm25:" + pm;
        default                      -> "unknown";
    };
}

long epochMillis = ...;
if (epochMillis instanceof int seconds) {
    // matches only if the value fits in int without loss
}
```

This converts a class of silent-truncation defects into non-matches, which are visible as an unexpected branch rather than as a corrupted value. Decoding a packed telemetry payload — a tag byte followed by fields of varying width — has exactly this shape, and a guarded `switch` over primitive patterns replaces a sequence of casts with range checks written by hand.

The feature remains preview. **JDK 26 is its fourth preview.** It therefore belongs behind `--enable-preview` in tooling, not in a deployable service, because preview semantics may change between releases and preview class files are rejected by a runtime of a different version.

### Implementation sketch (Scala)

Scala 3 has no primitive type patterns, but the lossless-conversion test that JEP 530 performs can be written explicitly. The sketch below shows the invariant — **narrow, widen back, and require the round trip to be the identity** — which is what makes the match total with respect to value preservation.

```scala
enum Reading:
  case Co2(ppm: Int)
  case Pm25(value: Float)
  case Raw(bits: Long)

/** True when `v` survives Long -> Int -> Long unchanged. */
def fitsInt(v: Long): Boolean = v.toInt.toLong == v

/** True when `d` survives Double -> Float -> Double unchanged.
  * NaN is special-cased because NaN == NaN is false. */
def fitsFloat(d: Double): Boolean =
  d.isNaN || d.toFloat.toDouble == d

def classify(reading: Reading): String = reading match
  case Reading.Co2(ppm) if ppm > 1500 => "co2-ventilate"
  case Reading.Co2(ppm)               => s"co2:$ppm"
  case Reading.Pm25(v)                => s"pm25:$v"
  case Reading.Raw(bits) if fitsInt(bits) => s"seconds:${bits.toInt}"
  case Reading.Raw(bits)                  => s"wide:$bits"
```

The guard placement is the point: the wide case must come **after** the narrowing case, because pattern alternatives are tried in source order and an unguarded `Raw` would shadow the narrowed one.

## Final-field mutation warnings (JEP 500)

JDK 26 emits a warning when code uses deep reflection to mutate a `final` field. The behaviour is selected by command-line flag:

```bash
# report occurrences without changing behaviour
java --illegal-final-field-mutation=warn -jar backend.jar

# reject the mutation
java --illegal-final-field-mutation=deny -jar backend.jar

# grant the capability while a dependency is updated
java --enable-final-field-mutation=ALL-UNNAMED -jar backend.jar
```

The affected population is libraries that rewrite fields after construction: serialization frameworks, mocking libraries, and transitive dependencies not visible in a direct dependency list. **Running a test suite under `deny` converts a future runtime failure into a present test failure**, which is the only way to find the offending library before a later release tightens the default.

## Remaining changes and what follows

**JEP 522** improves G1 throughput by reducing the synchronization its write barrier and refinement work require; no configuration change is needed to obtain it. **JEP 516** lifts the JDK 25 restriction that confined the ahead-of-time object cache to particular collectors, so the cache now applies under any garbage collector. The Applet API is removed outright (JEP 504).

JDK 27 is targeted for general availability in September 2026; its JEP list is fixed at rampdown but its contents are outside the scope of this article.

Separately, **JEP 401 (value classes) reached its first preview**, reported in August 2026. A `value class` gives up identity; `==` compares field values rather than references, and the virtual machine is permitted to flatten and scalarize instances. That is the largest change to the Java object model since generics, and its interaction with Scala case classes warrants its own treatment once early-access builds carrying it stabilise.

## Pitfalls

- **Setting `Version.HTTP_3` and assuming HTTP/3 is in use.** The version is a preference; a peer without HTTP/3 support silently yields an exchange over an earlier protocol version. Only `HttpResponse.version()` reports what was negotiated.
- **Expecting HTTP/3 to improve median latency.** Removing transport-level head-of-line blocking changes behaviour when packets are lost; on a loss-free path there is nothing to remove, and the median is unaffected.
- **Compiling primitive patterns with `--enable-preview` and shipping the class files.** Preview class files carry the minor version of the JDK that produced them and are rejected by any other runtime version, so an artifact built on JDK 26 preview fails to load on JDK 27.
- **Ordering a narrowing pattern after the wide fallback.** Alternatives are tried in source order; an unguarded wide case placed first shadows the narrowed case and the lossless check never runs.
- **Reading a JEP 500 warning as harmless because the process still runs.** Under the default `warn` mode the mutation succeeds; the same code fails under `deny`, which is the behaviour a later release moves toward.
- **Testing only direct dependencies for final-field mutation.** The mutation typically originates in a transitive dependency, so the warning appears at runtime under production workloads rather than during a dependency audit.
