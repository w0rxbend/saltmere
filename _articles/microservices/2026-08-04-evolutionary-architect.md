---
title: "The evolutionary architect: zone the city, don't design every building"
date: 2026-08-04
track: microservices
summary: "Sam Newman's town-planner metaphor reframes the architect's job from making decisions to setting principles and constraints, then guiding evolution. The practical half is making those constraints executable: here's a runnable ArchUnit fitness function that fails CI when one service reaches across a forbidden boundary."
reading_time: 5
tags: [evolutionary-architecture, fitness-functions, archunit, governance, paved-road, microservices]
sources:
  - title: "Building Microservices, 2nd ed. — Sam Newman (Ch. 16, The Evolutionary Architect)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Building Evolutionary Architectures — Ford, Parsons & Kua (nealford.com)"
    url: "https://nealford.com/books/buildingevolutionaryarchitectures.html"
  - title: "Fitness Functions — Building Evolutionary Architectures, Ch. 2 (O'Reilly)"
    url: "https://www.oreilly.com/library/view/building-evolutionary-architectures/9781491986356/ch02.html"
  - title: "ArchUnit User Guide (archunit.org)"
    url: "https://www.archunit.org/userguide/html/000_Index.html"
---

The word "architect" carries a lie borrowed from the construction industry. A building architect produces a blueprint precise enough that a structure gets built once, correctly, and then stands mostly unchanged for fifty years. Software is not like that, and pretending it is produces the failure mode everyone recognises: an architect who tries to specify every class, every framework, every table up front, hands the drawing to teams, and then spends the next two years watching reality diverge from the picture and calling the divergence "technical debt."

Sam Newman, in the *Evolutionary Architect* chapter of *Building Microservices*, swaps the metaphor. The better model isn't the architect who designs one building; it's the **town planner**. A town planner does not decide what goes inside each house. They zone the city — this district is residential, this one industrial, here is where the water and power run — and then let the buildings within each zone come and go as the inhabitants see fit. In microservice terms, the zones are the services, and the planner's attention belongs to *what happens between the zones*, not inside them. You care intensely about how services talk to each other, about the pipes and the protocols; you stay deliberately liberal about what any one team does inside its own boundary. Newman's rule of thumb is that inside the box you should be relaxed, and about the box's edges you should be firm.

## From making decisions to setting constraints

That reframing changes the job description. The architect stops being the person who makes the technical decisions and becomes the person who defines the space in which teams make their own. Newman splits that space into three tiers, and keeping them distinct is most of the discipline.

**Principles** are the small set of rules — he suggests fewer than ten — that align with what the organisation is actually trying to achieve. "We favour services that can be deployed independently" is a principle. **Practices** are the concrete, changeable ways you honour a principle right now: use HTTP/JSON for synchronous calls, emit logs in this structured format, run each service in its own container. Practices churn as tools change; principles should outlive them. **Constraints** are the things you genuinely cannot move — a regulator's data-residency rule, the one mainframe you must integrate with, the language your platform team can realistically support. The value of naming a constraint is that you can then *challenge* it honestly, rather than mistaking a temporary limitation for a law of nature. (This is also where the architect's real leverage over Conway's Law lives: the constraints you set on inter-service communication quietly shape the org that grows around them.)

## The paved road

Principles that live in a wiki get ignored. The move Newman pushes is to make the right way the *easy* way — what platform teams now call a **paved road** or golden path. Instead of a document telling teams to add health checks, metrics, structured logging and a circuit breaker, you ship an **exemplar** service that already has all of it, and a **service template** teams generate from. A team that takes the paved road gets observability and resilience for free; a team with an unusual need can still leave the road, they just carry the cost of paving their own. Governance stops being a review meeting and becomes something baked into the tools people already use.

## Make the architecture testable

Here is the part that separates aspiration from architecture. A principle you cannot check is a suggestion. Ford, Parsons and Kua, in *Building Evolutionary Architectures*, give the missing mechanism: the **fitness function**, borrowed from evolutionary computing. Their definition is deliberately plain — a fitness function "provides an objective integrity assessment of some architectural characteristic(s)." It answers, for one quality you care about, the question "are we still okay?" with a number or a pass/fail, automatically, on every change. They classify them along several axes — atomic versus holistic, triggered versus continual, static versus dynamic, automated versus manual — but the ones that actually protect an architecture are the automated, continual kind that run in CI and go red the moment someone drifts.

Newman's principles map straight onto this. "Services must not reach into each other's internals" isn't a paragraph in a standards doc; it's a test. On the JVM, the cleanest way to write that test is **ArchUnit** — a library that imports your compiled bytecode as data and lets you assert rules about packages and dependencies as ordinary JUnit tests.

Suppose your codebase is organised by bounded context — `com.acme.orders`, `com.acme.payments`, and so on — and the principle is that a context's `internal` package is private: other contexts talk to it only through its published API or via events. Encode it:

```java
package com.acme.arch;

import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;
import static com.tngtech.archunit.library.dependencies.SlicesRuleDefinition.slices;

@AnalyzeClasses(
        packages = "com.acme",
        importOptions = ImportOption.DoNotIncludeTests.class)
public class ArchitectureFitnessTest {

    // Constraint: payments' internals are private. Reach them through its API or events.
    @ArchTest
    static final ArchRule contexts_hide_their_internals =
            noClasses()
                    .that().resideOutsideOfPackage("com.acme.payments..")
                    .should().dependOnClassesThat()
                    .resideInAPackage("com.acme.payments.internal..")
                    .because("cross-context calls must go through the published API, not internals");

    // Principle: bounded contexts stay acyclic — no two services knot together.
    @ArchTest
    static final ArchRule contexts_are_acyclic =
            slices().matching("com.acme.(*)..")
                    .should().beFreeOfCycles();
}
```

`@AnalyzeClasses` tells ArchUnit which packages to import (skipping test classes); each `@ArchTest` field is a rule evaluated against the real bytecode. The first rule fails the build the instant any class outside `payments` imports, calls, or even references a type in `payments.internal`. The second uses ArchUnit's slice API to carve the code into one slice per context and assert there are no dependency cycles between them — the structural knot that quietly turns a distributed system back into a distributed monolith.

Wire this into CI as a normal test and the effect is that the principle now has teeth. A pull request that violates the boundary goes red before review, with a message naming the offending class and the exact forbidden access. The architect wrote the rule once; the pipeline enforces it on every commit forever, with no meeting. That is the whole shift Newman is arguing for, made concrete: you stopped policing an unwritten intention and started running an executable one, and the architecture is now free to evolve *underneath* a constraint that holds.

**Try next:** Pick the one cross-service boundary in your codebase you most wish people would stop crossing. Add `archunit-junit5` to a module, write a single `noClasses().that()...should().dependOnClassesThat()...` rule for it, and run it against `main` — if it's already red, you've just found your first fitness function's backlog; if it's green, you've locked the boundary in before it rots.
