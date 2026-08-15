---
title: "The evolutionary architect: zone the city, not every building"
date: 2026-08-04
track: microservices
summary: "Sam Newman's town-planner metaphor reframes the architect's job from making decisions to setting principles and constraints, then guiding evolution. The practical half is making those constraints executable: an ArchUnit fitness function that fails continuous integration when one service reaches across a forbidden boundary."
reading_time: 7
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

**Gist.** An architecture specified once, up front, diverges from the code the moment teams start building, and the divergence is invisible until someone reads the whole repository. Newman's *Evolutionary Architect* chapter replaces the blueprint with a small set of principles and constraints about what happens *between* services, and Ford, Parsons and Kua supply the enforcement mechanism: the **fitness function**, an automated check that answers "is this architectural characteristic still intact?" on every change. The cost is that every constraint worth holding must be reduced to something a machine can evaluate, and constraints that resist that reduction stay unenforced.

The construction metaphor imported with the word "architect" assumes a blueprint precise enough that a structure is built once and then stands mostly unchanged. The corresponding software failure mode is an architect who specifies classes, frameworks and tables up front, hands the drawing to teams, and then reclassifies every subsequent deviation as technical debt.

Newman substitutes the **town planner**. A planner does not decide what goes inside each house; the planner zones the city — residential here, industrial there, water and power along these routes — and lets buildings within a zone come and go. In microservice terms the zones are the services, and the planner's attention belongs to the traffic between zones rather than the contents of any one. Newman's rule of thumb: **be relaxed about what happens inside the box and firm about what happens at its edges**.

## From making decisions to setting constraints

The reframing changes the job description. The architect stops making the technical decisions and starts defining the space in which teams make their own. Newman splits that space into three tiers, and keeping them distinct is most of the discipline.

**Principles** are the small set of rules — he suggests around ten or fewer — aligned with what the organisation is trying to achieve. "Services must be independently deployable" is a principle. **Practices** are the concrete, changeable ways a principle is honoured at present: HTTP with JSON for synchronous calls, a specified structured log format, one container per service. Practices churn as tooling changes; principles are meant to outlive them. **Constraints** are the immovable facts — a regulator's data-residency rule, a mainframe that must be integrated with, the languages a platform team can support. Naming a constraint makes it available to be challenged, which distinguishes a temporary limitation from a fixed one.

The tiers fail when they are conflated. A practice mislabelled as a principle freezes a tool choice for years; a temporary limitation mislabelled as a constraint is never re-examined.

## The paved road

Principles that live only in a wiki are not enforced by anything. The mechanism Newman pushes is to make the correct path the path of least resistance — what platform teams call a **paved road** or golden path. Rather than a document instructing teams to add health checks, metrics, structured logging and a circuit breaker, the platform ships an **exemplar** service that already contains them and a **service template** from which teams generate new services. A team on the paved road inherits observability and resilience; a team with an unusual requirement may leave the road and absorb the cost of paving its own. Governance moves out of the review meeting and into the tooling teams already run.

The paved road covers what can be pre-packaged inside a service. It does not cover relationships *between* services, because no template can observe what a team imports six months later. That gap is what fitness functions close.

## Make the architecture testable

A principle that cannot be checked is a suggestion. Ford, Parsons and Kua define a fitness function as something that "provides an objective integrity assessment of some architectural characteristic(s)" — for one quality, a pass/fail or a number, produced automatically. They classify fitness functions along several axes: **atomic versus holistic** (one characteristic in isolation versus several in combination), **triggered versus continual**, **static versus dynamic**, and **automated versus manual**. The variants that protect a boundary in practice are the **automated, triggered ones wired into continuous integration**, because they turn red at the commit that causes the drift rather than at the audit months later.

Newman's principles map onto this directly. "Services must not reach into each other's internals" is expressible as a test. On the Java Virtual Machine (JVM) the mechanism is **ArchUnit**, a library that imports compiled bytecode as data and evaluates rules about packages and dependencies inside ordinary JUnit tests.

Working on bytecode rather than source is the load-bearing property: **the rule sees the dependency edges the compiler emitted**, including those introduced through field types, method signatures, thrown exceptions and constant references, not only the ones visible as import statements.

Given a codebase organised by bounded context — `com.acme.orders`, `com.acme.payments` — with the invariant that a context's `internal` package is reachable only through that context's published application programming interface (API) or via events:

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

`@AnalyzeClasses` selects the packages to import and excludes test classes; each `@ArchTest` field is a rule evaluated against the imported bytecode. The first rule fails as soon as any class outside `com.acme.payments` depends on a type in `com.acme.payments.internal`. The second uses the slice API to partition the code into one slice per context and asserts the absence of dependency cycles between slices — a cycle between two contexts means neither can be changed or released without regard for the other.

Run as a normal test in the pipeline, the rule fails a pull request before review, naming the offending class and the forbidden access. The architect states the constraint once; the pipeline evaluates it on every commit. The architecture remains free to evolve underneath a constraint that continues to hold.

### Implementation sketch (Scala)

The cycle check is a strongly connected component search over the slice graph. Tarjan's algorithm finds every cycle in **O(V + E)** for V slices and E inter-slice edges:

```scala
type Slice = String

/** Returned components with more than one member are dependency cycles. */
def stronglyConnected(edges: Map[Slice, Set[Slice]]): List[Set[Slice]] =
  var index = 0
  val idx, low = scala.collection.mutable.Map.empty[Slice, Int]
  val onStack = scala.collection.mutable.Set.empty[Slice]
  val stack = scala.collection.mutable.Stack.empty[Slice]
  val out = scala.collection.mutable.ListBuffer.empty[Set[Slice]]

  def visit(v: Slice): Unit =
    idx(v) = index; low(v) = index; index += 1
    stack.push(v); onStack += v
    for w <- edges.getOrElse(v, Set.empty) do
      if !idx.contains(w) then
        visit(w)
        low(v) = low(v) min low(w)
      // an edge to a node still on the stack closes a cycle
      else if onStack(w) then low(v) = low(v) min idx(w)
    if low(v) == idx(v) then
      val component = Set.newBuilder[Slice]
      var w = stack.pop(); onStack -= w; component += w
      while w != v do
        w = stack.pop(); onStack -= w; component += w
      out += component.result()

  edges.keys.foreach(v => if !idx.contains(v) then visit(v))
  out.result().filter(_.size > 1)
```

A slice with a self-edge is excluded by the `size > 1` filter, so intra-context dependencies do not register as violations.

## Pitfalls

- A rule written against source-level imports misses dependencies created by field types, method return types and thrown exception types; the boundary reads as clean while the bytecode carries the edge.
- A fitness function added to a codebase that already violates it fails on the first run, and the common response — deleting the rule — leaves the boundary permanently unguarded. The alternative is to freeze the existing violations explicitly and forbid new ones.
- A practice recorded as a principle outlives the tool it names: the transport or log format becomes non-negotiable years after the reason for choosing it has gone.
- Package-level rules do not constrain runtime coupling. Two contexts with no compile-time dependency can still be knotted together through a shared database table or a synchronous call built from a string URL, and no bytecode rule observes either.
- Cycle detection over slices reports the component, not the single edge that closed it; a component of eight contexts gives no indication of which recent dependency introduced the cycle.
- Constraints that resist automation — data residency, vendor lock-in — remain manual checks, and an architecture whose enforced rules are exactly the automatable ones drifts along every axis nobody could encode.
