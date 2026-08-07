---
title: "Parallel run: prove the new code in production before you trust it"
date: 2026-08-07
track: microservices
summary: "A parallel run calls both the old and new implementation on every live request, serves the old (trusted) result, and compares the new one in the background. It's how you verify a rewrite against real production traffic without risking a single response. Here's the pattern, a GitHub Scientist experiment, a language-agnostic comparator that handles side-effects and non-deterministic fields, and how to roll a real migration — plus why it is not a canary."
reading_time: 6
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

You've rewritten a gnarly piece of code — a pricing engine, a permissions check, a tax calculator — and your tests pass. Tests prove the cases you thought of. They say nothing about the malformed input a real customer sent in 2019 that some upstream system still replays, or the rounding edge your fixtures never covered. The only dataset that exercises all of that is production traffic, and you don't want to *serve* results from unproven code to find out.

A **parallel run** is the way out. On every live request you call *both* the old and the new implementation, you serve the old one's result (it's the trusted one), and you compare the new one's result in the background. Sam Newman puts it plainly in *Building Microservices*: "call both the old and new implementations simultaneously and compare their results." The user never sees the new code. You accumulate real-traffic evidence that it agrees with the old code — or a list of exactly the inputs where it doesn't — until you have the confidence to cut over.

Note the shape: this is a *verification* technique. It sits one level up from [parallel change / expand-contract](/articles/microservices/2026-08-03-parallel-change-expand-contract), which is a schema-and-interface migration pattern for coexisting old and new *shapes* of a contract — parallel run is about proving one *implementation* matches another before you switch, not about evolving a field.

## Not a canary

The distinction that trips people up is canary. Both run new code in production; they are opposites in who sees the result.

- A **canary** routes a *subset of live traffic* to the new path and **serves that path's result** to those users. Some real requests are answered by unproven code. You limit blast radius by limiting the fraction of users, and you watch error rates and latency to decide whether to widen it.
- A **parallel run** sends the *same request to both* paths and **serves the old, trusted result** to everyone. The new path answers nobody; its output only feeds a comparison. Blast radius is zero because no user is ever served the candidate.

So canary trades a little user exposure for a live signal; parallel run buys the signal with compute (you run the work twice) and gets zero exposure in return. They compose, too — a common sequence is parallel run to prove *correctness*, then canary to prove *operational* behavior under real serving load.

## The mechanics you can't skip

Two problems make a naive parallel run dangerous, and both are about the candidate touching the world.

**Side-effects.** If the new path also sends the email, charges the card, or writes the row, running it in parallel doubles those effects. The candidate must be side-effect-free, or run against a *shadow* store and stubbed collaborators. Newman's suggested tactic is a spy (from unit testing) so the new code *thinks* it sent the notification while you assert on the call instead of firing it. If you genuinely can't isolate the side-effects, you can't parallel-run that code — that's the honest constraint.

**Non-determinism.** Timestamps, generated IDs, map ordering, and floating-point tails will differ between two implementations that are both "correct." Compare naively and every request is a mismatch and the signal is noise. You normalize before comparing: drop or freeze `generated_at`, sort collections, round money to the cent.

## A GitHub Scientist experiment

GitHub open-sourced [Scientist](https://github.com/github/scientist/blob/main/README.md) — "a Ruby library for carefully refactoring critical paths" — which is a parallel run in library form. You declare a `use` block (the control, always returned) and a `try` block (the candidate). Scientist runs both, randomizes their order, swallows the candidate's exceptions, times both, compares the results, and hands a report to `publish`.

```ruby
require "scientist"

def can_read?(user, model)
  science "user-can-read" do |e|
    e.use  { legacy_permissions.check(user, model).allowed? }  # control: served
    e.try  { PolicyEngine.new(user).can?(:read, model) }        # candidate: compared only

    e.context user_id: user.id, model: model.class.name

    # Normalize before comparing so non-deterministic noise doesn't read as a mismatch.
    e.compare do |control, candidate|
      control == candidate
    end

    # Don't run the experiment for every request if it's hot; sample instead.
    e.run_if { rand < 0.10 }
  end
end
```

`science` returns whatever `use` returns — the candidate never changes what the caller gets. Order is randomized on each run specifically to surface hidden coupling: if the two blocks share mutable state, running control-then-candidate and candidate-then-control produce different mismatches. Flexport, who ran Scientist over real refactors, tracks exactly this as a **"Mismatched by run order"** metric to catch data inter-dependencies. Your `publish` method ships `result.matched?`, `result.mismatched?`, timings, and the serialized `context` to your metrics and logs — the mismatch log becomes your to-fix list. Ports exist well beyond Ruby: [Scientist.NET](https://github.com/scientistproject/Scientist.net), plus Python, Java, Go, Node/TypeScript, PHP, and more, so this is not a Ruby-only technique.

## The comparator, without a library

If no port fits your stack, a parallel run is about twenty lines. The load-bearing parts are the side-effect isolation and the normalization, not the framing:

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

function normalize(result):
    result.generated_at = null       # freeze non-deterministic fields
    result.trace_id     = null
    result.items        = sort(result.items)
    result.total        = round(result.total, 2)
    return result
```

The two rules that matter: `control` is what you return, on every branch including the error branch; and the candidate's cost — exceptions, latency, mismatches — is telemetry, never a user-facing failure.

## Rolling a real migration

1. **Wrap the seam behind an abstraction.** You need one interception point where both implementations can be invoked with the same input. Martin Fowler's [Branch By Abstraction](https://martinfowler.com/bliki/BranchByAbstraction.html) is the clean way to create it, and he notes Steve Smith's variation "which involves verifying that the two implementations return the same results to requests" — that variation *is* the parallel run.
2. **Make the candidate inert.** Audit it for side-effects and route every write, email, and external call to a shadow or a spy. This is usually the hardest engineering, and it's the step teams skip and regret.
3. **Start dark, sampled, comparison-only.** Turn the experiment on behind a flag at a low sample rate. Serve control, log mismatches, watch candidate latency and error rate.
4. **Drive mismatches to zero.** Each distinct mismatch is a real behavioral difference — a genuine bug in the new code, or (often) a latent bug in the old code, or a normalization gap. Fix, redeploy, watch the rate fall. Ramp the sample toward 100% as it stabilizes.
5. **Cut over deliberately.** When the match rate has held near-perfect across a full traffic cycle (weekday peak, weekend, month-end batch), flip the abstraction to serve the *candidate* and demote the old path to the compared side. Now you're de-risking the removal, not the addition.
6. **Contract.** Once the new path has served cleanly, delete the old implementation and the experiment scaffolding. A parallel run left running forever is just a permanent doubling of your compute bill.

The whole point is that at no moment before step 5 could the new code hurt a user, and by step 5 you have production-scale evidence — not a test suite's worth — that it behaves. That's a fundamentally stronger position than "the tests are green, ship it."

**Try next:** take one pure, side-effect-free function on a hot path you've been afraid to touch, wrap it with Scientist (or the twenty-line comparator above) at a 5% sample, and run it dark for a week — then read the mismatch log and see how many of the differences turn out to be bugs in the *old* code.
