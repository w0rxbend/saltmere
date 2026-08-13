---
title: "JDK 26 in Practice: HTTP/3, Primitive Patterns, and the Road Through 27 to Valhalla"
date: 2026-08-13
track: scala-jvm
summary: "A calendar correction first: JDK 26 shipped on 17 March 2026 — the September slot belongs to JDK 27, now in rampdown. Here's what JDK 26 actually delivered that's worth adopting on a telemetry backend: HTTP/3 in HttpClient (JEP 517), primitive types in patterns (JEP 530), and the final-field mutation warnings (JEP 500) you should audit for now."
reading_time: 5
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

Quick calendar check, because I got this wrong myself when planning upgrades: **JDK 26 is not "coming in September" — it shipped on 17 March 2026** with ten JEPs. The September 2026 slot belongs to **JDK 27**, which entered rampdown phase one on 4 June and is targeted for GA in mid-September. So as of August 2026, JDK 26 is the current release, five months into production use, and it's a good moment to sort what's actually worth adopting from what's preview noise.

## The JDK 26 scorecard

The ten JEPs: 500 (final-field mutation warnings), 504 (Applet API removed — finally), 516 (AOT object caching with any GC), 517 (HTTP/3 for HttpClient), 522 (G1 throughput via reduced synchronization), 524 (PEM encodings, second preview), 525 (structured concurrency, sixth preview), 526 (lazy constants, second preview), 529 (Vector API, eleventh incubation), and 530 (primitive types in patterns, fourth preview).

I've covered the earlier incarnations of structured concurrency, stable-values-now-lazy-constants, and the Vector API in the JDK 25 write-ups, and none changed enough in 26 to revisit (525 got an `onTimeout()` joiner and list-based return types; 529 is unchanged). The three below are the ones that changed how I write and run the sensor-fleet backend.

## HTTP/3 lands in HttpClient (JEP 517)

The standard `HttpClient` can now speak HTTP/3 over QUIC. Opting in is one builder call:

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

If the server doesn't offer HTTP/3, the client falls back to HTTP/2 — so this is safe to enable and observe. Why I care: the ingest tier talks to object storage and internal services over links where TCP head-of-line blocking is measurable under packet loss. QUIC moves the multiplexing into UDP streams, so one lost packet stalls one stream, not the whole connection. For fleet backends doing many small concurrent requests, that's the difference that shows up in p99, not p50. Bonus from the same release: `HttpRequest.BodyPublishers.ofFileChannel()` for streaming large bodies without loading them onto the heap.

## Primitive types in patterns (JEP 530, fourth preview)

Pattern matching finally treats `int`, `long`, `float` and friends as first-class in `instanceof` and `switch`. The killer semantic: a primitive type pattern matches only when the conversion is **lossless**, which turns a whole class of silent-truncation bugs into non-matches.

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

Decoding packed telemetry payloads is exactly this shape of code — a tag byte, then values of varying widths — and `switch` over primitives with guards is tidier and safer than the cast-and-check dance. Fourth preview means the design is stable (26 mostly improved dominance checking for `switch`); it re-runs as fifth preview in JDK 27, so I'd guess finalization lands in 28. Use it behind `--enable-preview` in tools, not in the deployable backend yet.

## Final means final — eventually (JEP 500)

JDK 26 starts warning when code uses deep reflection to mutate `final` fields. This is the integrity-by-default campaign continuing, and it will eventually break serialization frameworks, mocking libraries, and that one dependency you forgot about. The migration knobs:

```bash
# see what breaks today, without breaking it
java --illegal-final-field-mutation=warn -jar backend.jar

# test tomorrow's behavior
java --illegal-final-field-mutation=deny -jar backend.jar

# explicitly grant it while a library catches up
java --enable-final-field-mutation=ALL-UNNAMED -jar backend.jar
```

Run your test suite under `deny` now. The JDK 27 quality-outreach mail explicitly flags this as the thing to check before September. On my stack the offender was an older Jackson afterburner module; upgrading cleared it.

## Quick hits and what's next

**JEP 522** gives G1 dual card tables — 5–15% throughput improvement claimed, and it's on by default, so you get it for free. **JEP 516** makes the Leyden AOT cache GC-agnostic — the JDK 25 restriction where the cache only worked with certain collectors is gone, so ZGC users get fast starts too. Also gone: the Applet API (JEP 504) and `Thread.stop()`.

Looking ahead, **JDK 27** (GA mid-September 2026) finalizes compact object headers as the default (JEP 534), makes G1 the default GC everywhere (JEP 523), and ships post-quantum hybrid key exchange for TLS 1.3 (JEP 527). And the decade-long wait is over on Valhalla: **JEP 401, value classes, has been integrated as a preview into mainline for JDK 28** (announced this month). A `value class` gives up identity; `==` compares field values recursively, and the JVM is free to flatten and scalarize. That's the biggest object-model change since generics, and it will eventually matter enormously for Scala case classes on the JVM — a topic for its own article once the JDK 28 EA builds stabilize.

**Try next:** run your test suite on a JDK 27 EA build from jdk.java.net/27 with `--illegal-final-field-mutation=deny` and fix whatever it flags before September's GA forces the issue.
