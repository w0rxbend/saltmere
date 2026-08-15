---
title: "Parallel run: proving new code against production traffic before trusting it"
date: 2026-08-07
track: microservices
summary: "A parallel run invokes both the old and the new implementation on every sampled live request, serves the old (trusted) result, and compares the new one out of band. It verifies a rewrite against real production traffic without any request being answered by unproven code. Covers the pattern, a GitHub Scientist experiment, a language-agnostic comparator handling side effects and non-deterministic fields, the migration sequence, and why it is not a canary."
reading_time: 7
tags: [parallel-run, dark-launch, migration, testing-in-production, microservices]
sources:
  - title: "Branch By Abstraction — Martin Fowler (bliki)"
    url: "https://martinfowler.com/bliki/BranchByAbstraction.html"
  - title: "github/scientist — a Ruby library for carefully refactoring critical paths (README)"
    url: "https://github.com/github/scientist/blob/main/README.md"
  - title: "Building Microservices, 2nd Edition — Sam Newman"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Notes: Monolith to Microservices by Sam Newman — Edd Mann"
    url: "https://eddmann.com/posts/notes-monolith-to-microservices-by-sam-newman/"
  - title: "Using GitHub's Scientist library to refactor with confidence — Flexport Engineering"
    url: "https://flexport.engineering/using-githubs-scientist-library-to-refactor-with-confidence-9d34600edd5e"
---

**Gist.** A test suite exercises the cases its authors imagined; a rewritten pricing engine, permissions check or tax calculator meets inputs no fixture covers, and only production traffic exercises that distribution. A **parallel run** invokes both the old (control) and the new (candidate) implementation on the same request, returns the control's result, and compares the candidate's result out of band, so the candidate answers no user while accumulating real-traffic evidence. The cost is paid in compute — the work is performed twice — and in engineering effort to make the candidate side-effect-free, which is the constraint that decides whether the technique is applicable at all.

Sam Newman describes the pattern in *Building Microservices* as calling both the old and the new implementation rather than either one, so that their results can be compared. The output of the run is not a pass/fail verdict but a **list of the exact inputs on which the two implementations disagree**.

A parallel run is a *verification* technique. It sits one level above [parallel change / expand-contract](/articles/microservices/2026-08-03-parallel-change-expand-contract), which is a schema-and-interface migration pattern for coexisting old and new *shapes* of a contract. Parallel run proves that one *implementation* matches another before a switch; it does not evolve a field.

## Not a canary

Canary release and parallel run both execute new code in production. They are opposites in **who observes the new code's result**.

- A **canary** routes a subset of live traffic to the new path and **serves that path's result** to the users in the subset. Some real requests are answered by unproven code. Blast radius is bounded by the traffic fraction, and the decision to widen it comes from error rate and latency on the new path.
- A **parallel run** sends the *same request to both* paths and **serves the control's result to everyone**. The candidate's output feeds only a comparison. Blast radius is zero, because no user is ever served the candidate.

A canary trades user exposure for a live serving signal; a parallel run buys a correctness signal with duplicated compute and yields no exposure. The two compose: parallel run establishes *correctness*, canary then establishes *operational* behaviour under real serving load, which a parallel run cannot measure because the candidate never carries the serving path's dependencies at full rate.

## The two mechanics that cannot be skipped

Both hazards concern the candidate touching the world.

**Side effects.** If the candidate also sends the email, charges the card, or writes the row, executing it in parallel **doubles those effects**. The candidate must therefore be side-effect-free, or be pointed at a *shadow* store with stubbed collaborators. Newman's suggested tactic is the unit-testing spy: the candidate behaves as though it dispatched the notification, and the call is asserted on rather than performed. Where side effects cannot be isolated, the code cannot be parallel-run — that is the honest constraint, not a detail to engineer around later.

**Non-determinism.** Timestamps, generated identifiers, map iteration order and floating-point tails differ between two implementations that are both correct. Comparing raw results makes **every request a mismatch**, and the mismatch log stops carrying information. The comparison therefore runs over a normalized projection: non-deterministic fields dropped or frozen, collections sorted into a canonical order, monetary values rounded to the currency's minor unit.

## A GitHub Scientist experiment

GitHub open-sourced [Scientist](https://github.com/github/scientist/blob/main/README.md), described in its README as "a Ruby library for carefully refactoring critical paths". It is a parallel run in library form. An experiment declares a `use` block (the control, whose value is always returned) and a `try` block (the candidate). Scientist runs both, **randomizes their execution order**, swallows exceptions raised by the candidate, times both, compares the results, and passes a result object to `publish`.

```ruby
require "scientist"

def can_read?(user, model)
  science "user-can-read" do |e|
    e.use  { legacy_permissions.check(user, model).allowed? }  # control: served
    e.try  { PolicyEngine.new(user).can?(:read, model) }        # candidate: compared only

    e.context user_id: user.id, model: model.class.name

    # Normalize before comparing so non-deterministic noise does not read as a mismatch.
    e.compare do |control, candidate|
      normalize(control) == normalize(candidate)
    end

    # Sample rather than duplicating every request on a hot path.
    e.run_if { rand < 0.10 }
  end
end
```

`science` returns whatever `use` returns, so the candidate cannot alter what the caller receives. **Order randomization surfaces hidden coupling**: if the two blocks share mutable state, the control-then-candidate ordering and the candidate-then-control ordering produce different mismatches, and a difference that correlates with order is evidence of shared state rather than of divergent logic. Flexport, reporting on its use of Scientist over real refactors, describes tracking run order alongside mismatches for exactly this reason. The `publish` method forwards `result.matched?`, `result.mismatched?`, the timings and the serialized `context` to metrics and logs; the mismatch log is the work queue. Ports exist beyond Ruby, including [Scientist.NET](https://github.com/scientistproject/Scientist.net) alongside Python, Java, Go, Node/TypeScript and PHP implementations.

## The comparator without a library

Where no port fits the stack, the pattern is roughly twenty lines. The load-bearing parts are the side-effect isolation and the normalization, not the framing.

```
function computeWithParallelRun(request):
    control = oldImplementation(request)          # trusted; its result is returned

    if not sampled(request):                       # optional: don't double every request
        return control

    try:
        # Candidate must be side-effect-free. If it writes, point it at a
        # shadow store and stub collaborators so nothing escapes to the world.
        candidate = newImplementation(request, sideEffects = SHADOW)

        if normalize(control) != normalize(candidate):
            log.mismatch(
                key        = request.id,
                control    = normalize(control),
                candidate  = normalize(candidate),
                context    = request.summary())
            metrics.increment("parallel_run.mismatch")
        else:
            metrics.increment("parallel_run.match")
    catch err:
        # A candidate exception is data, never an incident — the user got `control`.
        metrics.increment("parallel_run.candidate_error")
        log.warn("candidate threw", err, request.id)

    return control        # ALWAYS the old result
```

Two invariants govern the whole construction. **`control` is returned on every branch, including the branch where the candidate raised.** And **the candidate's cost — its exceptions, its latency, its mismatches — is telemetry, never a user-visible failure.** Any implementation that violates the first invariant has become a canary without saying so.

### Implementation sketch (Scala)

The harness expresses both invariants in the type: the candidate is evaluated inside `Try`, and the control's value is the only value that escapes.

```scala
final case class Report[A](matched: Boolean, control: A, candidate: Try[A], context: Map[String, String])

def experiment[A](
    name: String,
    context: Map[String, String],
    sampleRate: Double,
    normalize: A => A,
    publish: Report[A] => Unit
)(control: => A, candidate: => A): A =
  if Random.nextDouble() >= sampleRate then control
  else
    val controlFirst = Random.nextBoolean() // order randomization exposes shared mutable state
    // Both thunks are forced here; the chosen order is the only difference between branches.
    val (c, k) =
      if controlFirst then
        val first = control
        (first, Try(candidate))
      else
        val first = Try(candidate)
        (control, first)

    val matched = k.map(normalize).toOption.contains(normalize(c))
    publish(Report(matched, c, k, context + ("control_first" -> controlFirst.toString)))
    c // the candidate's value and its exception both terminate here
```

`Try(candidate)` converts a candidate failure into a value, so a raised exception is recorded in the report rather than propagating to the caller. Recording `control_first` in the context is what makes the run-order metric computable after the fact.

## Rolling a migration

1. **Wrap the seam behind an abstraction.** A single interception point is required at which both implementations can be invoked with the same input. Martin Fowler's [Branch By Abstraction](https://martinfowler.com/bliki/BranchByAbstraction.html) describes how to create one: the callers are moved onto an abstraction layer, and the implementation behind that layer can then be swapped, or — as in a parallel run — invoked twice.
2. **Make the candidate inert.** Audit for side effects and route every write, notification and external call to a shadow or a spy. This is typically the largest engineering item in the migration.
3. **Start dark, sampled, comparison-only.** Enable the experiment behind a flag at a low sample rate, serving control, logging mismatches, and observing candidate latency and error rate.
4. **Drive mismatches to zero.** Each distinct mismatch is a real behavioural difference: a defect in the candidate, a latent defect in the control, or a gap in normalization. The sample rate is raised toward full traffic as the rate falls.
5. **Cut over deliberately.** Once the match rate has held across a full traffic cycle — weekday peak, weekend, month-end batch — the abstraction is flipped to serve the candidate and the old path is demoted to the compared side. The experiment now de-risks the *removal* rather than the addition.
6. **Contract.** After the new path has served cleanly, the old implementation and the experiment scaffolding are deleted. A parallel run left permanently enabled is a permanent duplication of compute.

Before step 5, no request can be harmed by the candidate; at step 5 the evidence is production-scale rather than fixture-scale.

## Pitfalls

- **A candidate that writes doubles the write.** Emails sent twice, cards charged twice, rows inserted twice — the symptom appears in production the moment the experiment is enabled, because "compared only" describes the *result*, not the code path's effects.
- **Comparing un-normalized results makes every request a mismatch.** A `generated_at` timestamp or a fresh trace identifier differs on every run, so the mismatch rate pins at 100% and the log carries no information about logic.
- **Sorting inside `normalize` hides an ordering bug.** If the contract specifies an order and the comparator sorts before comparing, a candidate that returns the right elements in the wrong order is recorded as a match.
- **Returning the candidate on the control's error branch converts the experiment into a canary.** The blast-radius-zero property holds only if `control` is returned unconditionally.
- **An unsampled experiment on a hot path doubles that path's load.** Latency regressions and dependency saturation follow from the duplicated work, not from the candidate's logic.
- **Mismatches attributed only to the candidate mask defects in the control.** A difference is evidence that the two disagree; deciding which is correct requires reading the specification, and the old implementation is frequently the wrong one.
- **Order-correlated mismatches indicate shared mutable state, not divergent logic.** Without recording which block ran first, the two causes are indistinguishable in the mismatch log.
- **An experiment left enabled after cut-over keeps paying for the dead implementation.** The compute duplication persists indefinitely, and the retained old code continues to require maintenance.
