---
title: "JEP 513: statements before super() without the static-helper detour"
date: 2026-07-31
summary: "A Java constructor's first statement had to be super(...) or this(...), so validating or transforming a superclass argument required a static helper or an inline expression. JEP 513, final in JDK 25, permits ordinary statements before the delegation. This article covers what the prologue may and may not touch, the early construction context rule, the three preview rounds that preceded standardisation, and the corresponding Scala idiom."
track: scala-jvm
reading_time: 6
tags: [java, jdk25, jep-513, constructors, language-design, scala-jvm]
sources:
  - title: "JEP 513: Flexible Constructor Bodies"
    url: "https://openjdk.org/jeps/513"
  - title: "JEP 447: Statements before super(...) (Preview)"
    url: "https://openjdk.org/jeps/447"
  - title: "JDK 25 (project page, final feature list)"
    url: "https://openjdk.org/projects/jdk/25/"
  - title: "Flexible Constructor Bodies in Java 25 (Baeldung)"
    url: "https://www.baeldung.com/java-25-flexible-constructor-bodies"
---

**Gist.** Java required the explicit constructor invocation — `super(...)` or `this(...)` — to be the literal first statement of a constructor body, which pushed argument validation and argument preparation into `static` helper methods or into expressions nested inside the invocation itself. **JEP 513, delivered as a final standard feature in JDK 25**, allows ordinary statements to precede the invocation. The cost is a new set of compile-time restrictions: the statements that run before the delegation execute in an **early construction context**, where the instance under construction cannot be read, passed, or used as a method receiver.

## The constraint that produced the helper methods

The rule being relaxed is a syntactic one: the explicit constructor invocation had to appear first. A subclass that must reject an argument before the superclass observes it therefore could not write the test as a statement. The test was instead relocated into a `static` method invoked as the argument expression, because argument evaluation happens before the delegation:

```java
// Before JDK 25 — validation relocated into a static helper
public class PositiveNumber extends Number {
    public PositiveNumber(int value) {
        super(verify(value));   // must be the first statement
    }
    private static int verify(int value) {
        if (value <= 0)
            throw new IllegalArgumentException("must be positive: " + value);
        return value;
    }
}
```

The helper carries no meaning of its own; it exists to satisfy the ordering rule. Two other workarounds have the same origin: a conditional expression embedded in the argument list, and validation performed *after* `super(...)` returns, which runs the superclass constructor to completion before the failure is detected.

## The two regions of a constructor body

JEP 513 splits the body into two regions. The **prologue** is the sequence of statements before the `super(...)` or `this(...)` invocation; the **epilogue** is everything after it. Ordinary statements are now legal in the prologue:

```java
// JDK 25 — validation stated as a statement, no helper
public class PositiveNumber extends Number {
    public PositiveNumber(int value) {
        if (value <= 0)
            throw new IllegalArgumentException("must be positive: " + value);
        super(value);
    }
}
```

The JEP names three uses for the prologue: **validating** arguments before the superclass constructor runs, **preparing** arguments — computing a derived value or making a defensive copy — and **sharing** logic across `this(...)` delegations.

## The invariant: the instance is not observable in the prologue

The prologue executes in what the JEP calls an **early construction context**. Within it, `this` may not be used, explicitly or implicitly: no reading of fields, no invocation of instance methods, no passing of the instance to anything else. The reason is structural rather than stylistic — the superclass constructor has not yet run, so the superclass's own fields hold their default values and any superclass invariant is not yet established. The restriction is enforced by the compiler, so the failure mode is a compile error rather than an object observed in a partially initialized state.

One case is permitted inside the early construction context: **the prologue may assign to a field declared in the class being instantiated.** Fields inherited from a superclass remain off limits.

```java
public class Employee extends Person {
    private final String officeID;
    public Employee(int age, String officeID) {
        if (age < 18 || age > 67)
            throw new IllegalArgumentException("age out of range: " + age);
        this.officeID = officeID;   // write to own field before super() — allowed
        super(age);
    }
}
```

The write is admissible because it is a write to a field of the class being constructed, not a read of the instance. Reading `this.officeID` in the same prologue, or invoking any instance method, remains a compile error. The asymmetry is the operative rule: **writes to the class's own uninitialized fields are allowed, reads of the instance are not.**

The practical consequence of the write permission concerns overridable methods. When a superclass constructor calls a method that the subclass overrides, that override runs before the subclass constructor body — a long-standing hazard, since the override sees default field values. Assigning a subclass field in the prologue is a mechanism whose effect is visible to such an override, because the assignment happens before the superclass constructor is entered.

### Implementation sketch (Scala)

Scala reaches the same outcome through a different route: the primary constructor's parameter list is evaluated in the subclass before delegation, and **statements in the class body run after the superclass constructor**, so a validation written in the body is a post-construction check. Expressing a pre-delegation check requires moving it into the expression handed to the parent, or into a factory:

```scala
abstract class Number1(val intValue: Int)

// Post-construction check: the parent constructor has already run.
class LateChecked(v: Int) extends Number1(v):
  require(v > 0, s"must be positive: $v")   // runs after Number1's constructor

// Pre-delegation check: evaluated while computing the parent's argument.
class EagerChecked(v: Int) extends Number1(
  if v > 0 then v else throw IllegalArgumentException(s"must be positive: $v")
)

// The idiom that keeps the check as a statement: a private constructor
// plus a companion factory, so validation precedes construction entirely.
class Positive private (val value: Int) extends Number1(value)

object Positive:
  def apply(v: Int): Positive =
    if v <= 0 then throw IllegalArgumentException(s"must be positive: $v")
    new Positive(v)

  def either(v: Int): Either[String, Positive] =
    if v <= 0 then Left(s"must be positive: $v") else Right(new Positive(v))
```

The factory form is the closest analogue of a JDK 25 prologue: validation is a statement, it precedes any superclass work, and — in the `either` variant — the failure is a value rather than an exception.

## Preview history

The change was standardised after three preview rounds:

- **JEP 447** — JDK 22, first preview, titled *Statements before super(...)*
- **JEP 482** — JDK 23, second preview, renamed *Flexible Constructor Bodies*
- **JEP 492** — JDK 24, third preview
- **JEP 513** — JDK 25, **final**, requiring no `--enable-preview` flag

JDK 25 is a long-term-support (LTS) release, so the feature is available without a preview flag in a release intended for long-lived code. The observable effect is narrow: constructors read in the order they execute, and the class of `static` helper methods that existed only to satisfy the ordering rule can be removed.

## Pitfalls

- **Reading a field written earlier in the same prologue does not compile.** `this.officeID = id; if (this.officeID == null) ...` fails: the write is permitted, the read is a use of `this` in the early construction context.
- **A field initializer overwrites what the prologue assigned.** Instance initializers and field initializers run immediately after `super(...)` returns, so `private String id = "";` combined with `this.id = x;` in the prologue leaves `id` equal to `""`. For a `final` field carrying an initializer the same collision is a compile error instead, under the ordinary rule that a final field is assigned exactly once.
- **An exception thrown from the prologue escapes before the superclass constructor has run**, whereas the pre-25 post-`super(...)` check throws only after the superclass part is fully constructed — the two forms differ in what work has already been done when the failure surfaces.
- **A superclass constructor that calls an overridable method still observes default values for every subclass field the prologue did not assign.** Moving one assignment into the prologue does not make the override safe; it changes which single field is visible.
- **Source compiled with `--release 21` or lower rejects prologue statements** even on a JDK 25 compiler, because the restriction is a language-level rule, not a runtime one.
- **The Scala class body is not a prologue.** A `require` written in the body of a subclass runs after the superclass constructor has completed, so it cannot prevent superclass initialization the way a JDK 25 prologue check can.
