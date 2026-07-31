---
title: "JEP 512: Java Finally Lets You Write void main()"
date: 2026-07-31
track: scala-jvm
summary: "In JDK 25, compact source files and instance main methods went from four rounds of preview to a permanent, standard feature. Here is what actually changed for beginners' first Java program, and why it doesn't touch anything larger."
reading_time: 4
tags: [java, jdk25, jep-512, language-design, onboarding]
sources:
  - title: "JEP 512: Compact Source Files and Instance Main Methods"
    url: "https://openjdk.org/jeps/512"
  - title: "JDK 25 (project page, feature list)"
    url: "https://openjdk.org/projects/jdk/25/"
  - title: "Java 25 / JDK 25: General Availability (announce list, 16 Sep 2025)"
    url: "https://mail.openjdk.org/pipermail/announce/2025-September/000360.html"
  - title: "IO (Java SE 25 & JDK 25 API docs)"
    url: "https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/io/IO.html"
  - title: "InfoQ: JDK 25 Finalizes Instance Main Methods"
    url: "https://www.infoq.com/news/2025/05/jdk25-instance-main-methods"
---

For twenty-five years, the first thing anyone learned about Java was a lie of omission: `public static void main(String[] args)`. Every token in that line is something a beginner cannot yet explain — visibility, static dispatch, return types, array parameters — and all of it stands between them and printing one string. **JEP 512, delivered as a final feature in JDK 25 (GA 16 September 2025), removes that wall.** No preview flag, no `--enable-preview`. It's just Java now.

## The evolution: four previews, then permanent

This did not land overnight. The design ran a full preview cycle so the ergonomics could be tested against real feedback before becoming permanent:

- **JEP 445** — JDK 21 (first preview, then called "unnamed classes and instance main methods")
- **JEP 463** — JDK 22 (second preview)
- **JEP 477** — JDK 23 (third preview, added the auto-imports)
- **JEP 495** — JDK 24 (fourth preview)
- **JEP 512** — JDK 25 (**final**)

Because JDK 25 is an LTS release (Oracle support through at least September 2033), the compact form is now something you can rely on long-term rather than an experiment.

## Before and after

Traditional HelloWorld — the version still in a million tutorials:

```java
public class HelloWorld {
    public static void main(String[] args) {
        System.out.println("Hello, World!");
    }
}
```

The compact form in JDK 25:

```java
void main() {
    IO.println("Hello, World!");
}
```

That is a complete, runnable program. Every deletion is deliberate.

## What each piece buys you

**No class declaration.** Code written at the top level of a source file becomes an *implicitly declared class* — the compiler wraps it in an unnamed top-level class for you. The beginner never writes `class` and never has to reconcile the class name with the filename.

**An instance `main`, not `static`.** The launcher will accept `void main()` as an entry point. Because it's an instance method, the runtime constructs the implicit class (via its no-arg constructor) and then calls `main` on that instance. No `static`, so no need to explain why `main` can't touch instance state — there just isn't a static/instance split to trip over yet.

**No `String[] args`.** The launcher resolves `main` in a defined priority order, preferring a `main` with no parameters over one taking `String[]`. You add the args parameter the day you actually need command-line arguments, not before.

**No `public`.** Access modifiers are simply optional here, so `void main()` is enough.

**Auto-imported `java.io.IO`.** JDK 25 ships a small `java.io.IO` class with static `println`, `print`, and `readln` methods, and it's implicitly available in compact source files — no import, no `System.out`. `IO.readln("Your name? ")` prints a prompt and returns a line from standard input; `IO.println(x)` writes a line. It's a deliberately tiny console API for the on-ramp.

```java
void main() {
    String name = IO.readln("What's your name? ");
    IO.println("Hello, " + name);
}
```

**Auto-imported `java.base`.** Compact source files behave as if every public top-level class and interface exported by the `java.base` module were on-demand imported. `List`, `Map`, `Path`, `File` and friends are usable straight away, so the first program that needs a collection doesn't need an import line to explain first.

## Running it

Pair this with the single-file source launcher (present since JDK 11) and the whole ceremony collapses to one command:

```
java Hello.java
```

The launcher compiles and runs the file in memory — no `javac`, no `.class` on disk. You can also force source mode explicitly with `java --source 25 Hello.java`, which is handy when the file has no `.java` extension (shebang-style scripts). The compact `void main()` form makes these single-file programs read like a script while still being ordinary Java.

## Why this doesn't change the language for real programs

The important design property: **compact source files are a strict superset entry point, not a new dialect.** The instant a file contains an explicit `class` (or any other top-level type declaration), the compact rules switch off and normal Java applies. `System.out.println` still works. `public static void main(String[] args)` still works and is still what you'll see in production code and frameworks. There's no migration, no deprecation, and nothing to relearn when a beginner graduates to a multi-class project — they simply add the declarations they were implicitly getting.

In other words, JEP 512 lowers the first step without lowering the ceiling. The five-line program grows into a real one by *adding* structure, and every line the learner already wrote stays valid.

**Try next:** Install a JDK 25 build, save the compact `void main()` snippet above as `Hello.java`, and run `java Hello.java` with no compile step. Then add a second method that `main` calls, and finally wrap everything in an explicit `public class Hello { ... }` — watch which parts you now have to spell out yourself, and you'll have felt exactly what the implicit top-level class was doing for you.
