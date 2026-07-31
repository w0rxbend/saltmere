---
title: "JEP 513: run code before super() without the static-helper dance"
date: 2026-07-31
track: scala-jvm
summary: "For 25 years a Java constructor's very first statement had to be super(...) or this(...) — so validating or transforming a superclass argument meant a static helper method or an unreadable inline expression. JEP 513, final in JDK 25, lets you write ordinary statements before the delegation. Here's what the prologue can and can't touch, the four-release preview history, and a before/after refactor."
reading_time: 5
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

Every Java programmer has hit this wall: you're writing a constructor, you need to check an argument before handing it to the superclass, and the compiler tells you `call to super must be first statement in constructor`. The language forced `super(...)` (or `this(...)`) to be the literal first thing in the body, so any pre-flight validation, argument massaging, or defensive copy had to be smuggled into the `super(...)` call itself or hidden behind a `static` helper. **JEP 513, delivered as a final, standard feature in JDK 25 (GA 16 September 2025)** — no `--enable-preview` — removes the wall.

## Why the old rule was a wart

Suppose a `PositiveNumber` subclass must reject non-positive values before the superclass ever sees them. Pre-25, you couldn't write the `if` first, so you reached for a static method that runs *as* the argument is evaluated:

```java
// Before JDK 25 — validation smuggled into a static helper
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

The helper exists only to dodge a syntax rule. Worse patterns abound: cramming a ternary into the `super(...)` argument, or letting the superclass construct a half-valid object and validating *after*, which wastes the superclass's initialization work when the check fails.

## What JEP 513 actually allows

The constructor body now splits into two parts. The **prologue** is the code *before* the `super(...)`/`this(...)` invocation; the **epilogue** is the code after. You may put ordinary statements in the prologue:

```java
// JDK 25 — validate up front, no helper
public class PositiveNumber extends Number {
    public PositiveNumber(int value) {
        if (value <= 0)
            throw new IllegalArgumentException("must be positive: " + value);
        super(value);
    }
}
```

Same effect, but the intent reads top-to-bottom and the throwaway method is gone. The prologue is exactly the place for the three things you always wanted to do early: **validate** arguments (fail before touching the superclass), **prepare** them (compute a derived value, defensively copy a collection), and **share** logic between `this(...)` delegations.

## The safety rule: no peeking at a half-built object

The prologue runs in what the JEP calls an **early construction context**, and the constraint is strict: you cannot use `this` — explicitly or implicitly — to read fields, call instance methods, or leak the object, because the instance genuinely isn't initialized yet (the superclass constructor hasn't run). This is what keeps the feature safe; it never lets you observe an object in a broken state.

There is one deliberate exception. The prologue *may* assign to fields declared in the same class, as long as those fields have no initializer:

```java
public class Employee extends Person {
    private final String officeID;
    public Employee(int age, String officeID) {
        if (age < 18 || age > 67)
            throw new IllegalArgumentException("age out of range: " + age);
        this.officeID = officeID;   // assign own field before super() — allowed
        super(age);
    }
}
```

Assigning `this.officeID` before `super(...)` is legal because it's a plain write to this class's own field, not a *read* of the under-construction instance. Reading `this.officeID`, or calling any instance method, in the prologue still won't compile.

## Four releases to get here

This landed the way risky language changes should — a long preview so the JLS rules could be validated against real code before becoming permanent:

- **JEP 447** — JDK 22, first preview, titled *Statements before super(...)*
- **JEP 482** — JDK 23, second preview, renamed *Flexible Constructor Bodies*
- **JEP 492** — JDK 24, third preview
- **JEP 513** — JDK 25, **final**

Because JDK 25 is an LTS release, flexible constructor bodies are now something you can commit to in long-lived code rather than an experiment behind a flag. The payoff is small but pervasive: constructors finally read in the order they execute, and a category of static helper methods that existed purely to satisfy the compiler can disappear.

**Try next:** On a JDK 25 build, take one of your existing constructors that calls a `private static` validation helper inside `super(...)`, and inline the check as a prologue `if` before the `super(...)` call. Then try to *read* a field or call an instance method in that prologue and watch the compiler reject it — that error is the early-construction-context rule protecting you from observing a half-initialized object.
