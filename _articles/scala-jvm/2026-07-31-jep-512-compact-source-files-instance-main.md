---
title: "JEP 512: Compact Source Files and Instance Main Methods"
date: 2026-07-31
track: scala-jvm
summary: "In JDK 25, compact source files and instance main methods moved from four rounds of preview to a permanent, standard feature. What changed in the first Java program, and why nothing larger is affected."
reading_time: 6
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

**Gist.** The canonical Java entry point, `public static void main(String[] args)`, requires a learner to accept four concepts — visibility modifiers, static dispatch, array types, and a class declaration whose name must match the file — before printing one string. **JEP 512, delivered as a final feature in JDK 25 (general availability 16 September 2025)**, lets a source file declare methods and fields at the top level and lets the launcher accept an instance method `void main()` as the entry point, with `java.io.IO` and the `java.base` module implicitly imported. The cost is a second set of rules that applies only to files written in this shape: code moved out of a compact source file into an ordinary class loses the implicit imports and must spell out what the compiler had been supplying.

## Preview cycle

The feature ran a full preview cycle under successive JEP (JDK Enhancement Proposal) numbers before being made permanent:

- **JEP 445** — JDK 21 (first preview, then titled "unnamed classes and instance main methods")
- **JEP 463** — JDK 22 (second preview)
- **JEP 477** — JDK 23 (third preview, added the implicit imports)
- **JEP 495** — JDK 24 (fourth preview)
- **JEP 512** — JDK 25 (**final**)

JDK 25 is a long-term-support (LTS) release. A compact source file therefore no longer requires `--enable-preview` at compile and run time, which is the practical difference from JDK 21 through 24.

## Before and after

The traditional form, which remains valid:

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

Both are complete, runnable programs. Four distinct rules account for the difference.

## The four rules

**Implicit class declaration.** Methods and fields written at the top level of a source file become members of an *implicitly declared class*. **The implicit class has no name that can be written in source**, so no other compilation unit can refer to it, and the filename is not constrained by a class name. The class is the compilation unit's own top-level type; the file may still be launched by path.

**Instance `main`, not `static`.** The launcher accepts `void main()` as an entry point. Because it is an instance method, **the launcher instantiates the implicit class through its no-argument constructor and then invokes `main` on that instance**. The static/instance distinction — and the rule that a static method cannot read instance state — does not arise in the first program.

**Parameterless `main`.** The launcher resolves among candidate `main` methods in a defined priority order, and a `main` declared with no parameters is a valid candidate. A `String[]` parameter is added when command-line arguments are needed, not before. Access modifiers are optional in this position, so `void main()` suffices without `public`.

**Implicit imports.** JDK 25 ships `java.io.IO`, a small class with static `println`, `print` and `readln` methods, and makes it available in compact source files with no import declaration and no `System.out`. `IO.readln("Your name? ")` writes a prompt and returns one line from standard input; `IO.println(x)` writes a line. Separately, **a compact source file behaves as though every public top-level class and interface exported by the `java.base` module were imported on demand**, so `List`, `Map`, `Path` and `File` resolve without an import line.

```java
void main() {
    String name = IO.readln("What's your name? ");
    IO.println("Hello, " + name);
}
```

## Running a single file

The single-file source launcher, present since JDK 11, compiles and runs a source file in one step:

```
java Hello.java
```

**No `javac` invocation and no `.class` file on disk**: the compilation result is held in memory for the duration of the run. Source mode can be forced explicitly with `java --source 25 Hello.java`, which is the form required when the file has no `.java` extension — for example a shebang-style script. Combined with the compact form, a single-file program reads like a script while remaining ordinary Java.

## Scope of the change

The feature adds an entry point shape; it does not define a dialect. `System.out.println` continues to work. `public static void main(String[] args)` continues to work and remains the form found in production code and framework-generated projects. Nothing is deprecated, and no migration is implied. **Growth from a compact source file to a multi-class program proceeds by adding declarations that were previously implicit**, not by rewriting the code already present.

### Implementation sketch (Scala)

Scala 3 addresses the same first-program problem with a different mechanism, worth contrasting because both target the JVM. Top-level definitions are permitted directly in a source file, and the `@main` annotation marks a method as a launchable entry point; **the command-line arguments are converted to the annotated method's declared parameter types rather than handed over as `Array[String]`**.

```scala
// Hello.scala — top-level definitions, no enclosing object required.

def greeting(name: String): String = s"Hello, $name"

@main def hello(): Unit =
  println(greeting("World"))

// A second entry point in the same file; arguments are parsed to the
// declared types, and a conversion failure is reported before the body runs.
@main def repeat(name: String, times: Int): Unit =
  (1 to times).foreach(_ => println(greeting(name)))
```

Two differences from JEP 512 are load-bearing. First, **the entry point is selected by annotation, not by method name**, so a file may declare several and the launched one is named on the command line. Second, **argument conversion is part of the mechanism**: `repeat World three` fails on the `Int` conversion rather than inside the method body. Java's compact source file keeps the `main` name and leaves argument handling to the program.

## Pitfalls

- **Implicit imports do not survive refactoring.** Moving `IO.println(...)` from a compact source file into an explicit class produces a compile error naming `IO` as an unresolved symbol: the implicit `java.io.IO` and `java.base` imports are a property of compact source files, not of the language generally. The fix is an explicit `import java.io.IO;`.
- **A compact source file does not compile on JDK 21 through 24 without a flag.** The same text requires `--enable-preview` (and a matching `--release`/`--source`) on those releases, and fails outright on JDK 20 and earlier. The implicit imports arrived only with the third preview in JDK 23, so a file relying on `IO` or on unimported `java.base` types does not compile under JDK 21 or 22 even with the flag. A file that runs under JDK 25 is not portable backwards.
- **The implicit class cannot be referenced by name.** Because it has no writable name, other source files cannot import it, extend it, or name it in a test. Code intended for reuse belongs in an explicitly declared class from the start.
- **The implicit class is instantiated before `main` runs.** Any initialisation that fails during construction — a field initialiser that throws — surfaces before the first line of `main` executes, which is a different failure point from the static entry form.
- **`java Hello.java` leaves no build output.** Tooling that expects a `.class` file, a jar, or a stable output directory finds nothing, because the source launcher keeps the compiled form in memory only.
