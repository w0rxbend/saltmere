---
title: "OpenFeature: feature flags without a vendor-specific API"
date: 2026-07-30
track: microservices
summary: "Feature-flag SDKs are sticky: a vendor call such as GetFlag(...) written at every call site makes switching backends a rewrite. OpenFeature is a CNCF vendor-neutral flag API — application code calls a standard client, and a swappable provider talks to the backend. This covers the model, the Go and Java shape, and where hooks fit."
reading_time: 6
tags: [openfeature, feature-flags, cncf, progressive-delivery, providers, go]
sources:
  - title: "OpenFeature — official site and specification"
    url: "https://openfeature.dev/"
  - title: "OpenFeature becomes a CNCF incubating project — CNCF blog (Dec 2023)"
    url: "https://www.cncf.io/blog/2023/12/19/openfeature-becomes-a-cncf-incubating-project/"
  - title: "OpenFeature Specification — evaluation API, providers, hooks"
    url: "https://openfeature.dev/specification/"
  - title: "open-feature/go-sdk — GitHub"
    url: "https://github.com/open-feature/go-sdk"
  - title: "Feature Toggles (aka Feature Flags) — Pete Hodgson, martinfowler.com"
    url: "https://martinfowler.com/articles/feature-toggles.html"
---

**Gist.** Feature flags separate *deploy* from *release* — code ships dark and is enabled for a subset of users, or disabled instantly — but a vendor SDK invoked at every call site makes the vendor a compile-time dependency of the whole codebase. OpenFeature, a **CNCF incubating project since December 2023**, interposes a vendor-neutral evaluation API between application code and the flag backend, so the backend is selected at one registration point instead of at hundreds of call sites. The cost is that only *evaluation* is standardised: flag authoring, targeting-rule syntax and analytics remain backend-specific, and the indirection adds a hook pipeline and a default-value contract that must be reasoned about on every call.

## The lock-in being removed

The concept of a toggle is not the difficulty; Hodgson's taxonomy of release, experiment, ops and permission toggles predates any standard. The difficulty is that a call such as `launchdarkly.BoolVariation("new-checkout", ...)` appearing at two hundred sites encodes the vendor's package name, its argument order and its error model into every module that gates behaviour. Changing backend, or running one backend in staging and another in production, then requires editing every site.

## The three moving parts

**Provider** — the adapter that translates the standard evaluation call into a backend protocol. It is registered through the global API at startup, either as the default provider or bound to a named domain.

**Client** — the object application code evaluates against. It is backend-agnostic and delegates to the currently registered provider.

**Evaluation context** — the per-evaluation subject description: targeting key plus attributes such as plan tier or region, on which the backend's targeting rules operate.

The complete shape in Go:

```go
import (
    "context"
    "github.com/open-feature/go-sdk/openfeature"
    flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
)

func main() {
    // 1. Register a provider ONCE at startup (this line selects the backend).
    openfeature.SetProvider(flagd.NewProvider())

    // 2. Obtain a client anywhere in the application.
    client := openfeature.NewClient("checkout-service")

    // 3. Build context for this subject, then evaluate with a safe default.
    evalCtx := openfeature.NewEvaluationContext(
        "user-4711",
        map[string]interface{}{"plan": "pro", "country": "UA"},
    )
    enabled, _ := client.BooleanValue(
        context.Background(), "new-checkout", false /* default */, evalCtx,
    )

    if enabled {
        newCheckout()
    } else {
        legacyCheckout()
    }
}
```

Two properties carry the design. First, **every evaluation supplies a default value** (`false` above). If the provider is unreachable or the flag is undefined, the default is returned rather than an exception propagated, so an unavailable flag service degrades to the default configuration instead of failing requests. Second, **the only backend-specific line is `SetProvider`**; the remainder is portable across every OpenFeature backend. The Java, Node, Python and .NET SDKs mirror the same call shape — in Java, `client.getBooleanValue("new-checkout", false, ctx)`.

## Hooks: the cross-cutting layer

Hooks are the specification's extension points around each evaluation, with four stages: **`before`, `after`, `error` and `finally`**. They allow flag evaluation to be connected to the rest of the platform without editing call sites:

- Emit an OpenTelemetry span or metric per evaluation, so a behavioural change can be attributed to a specific flag and variant.
- Enrich the evaluation context automatically, for example by injecting the trace identifier or the authenticated subject.
- Log or validate the evaluation.

Hooks are registered at four levels — the global API, the client, the individual invocation and the provider — so a hook registered above the call site covers every evaluation performed through it. This is what distinguishes the specification from a lowest-common-denominator wrapper: the telemetry and context plumbing is part of the standard, not left to each adapter.

### Implementation sketch (Scala)

The load-bearing mechanism is not the transport but the evaluation pipeline: context enrichment, delegation to a provider that may fail, and unconditional substitution of the default. A standard-library model makes the invariant explicit.

```scala
final case class EvalContext(targetingKey: String, attrs: Map[String, String])

/** Backend adapter. May throw or time out; the client must absorb that. */
trait Provider:
  def resolveBoolean(flag: String, ctx: EvalContext): Boolean

trait Hook:
  def before(flag: String, ctx: EvalContext): EvalContext = ctx
  def after(flag: String, value: Boolean): Unit = ()
  def error(flag: String, t: Throwable): Unit = ()

final class Client(provider: Provider, hooks: List[Hook]):

  def booleanValue(flag: String, default: Boolean, ctx: EvalContext): Boolean =
    // before-hooks run in registration order and may enrich the context
    val enriched = hooks.foldLeft(ctx)((c, h) => h.before(flag, c))
    try
      val v = provider.resolveBoolean(flag, enriched)
      hooks.reverse.foreach(_.after(flag, v)) // after-hooks unwind in reverse
      v
    catch
      case t: Throwable =>
        hooks.reverse.foreach(_.error(flag, t))
        default // invariant: the caller never observes a provider failure
```

The `catch` clause is the whole fail-safe contract: an evaluation returns either a resolved variant or the caller's default, never a propagated fault. The consequence is that a misconfigured or unreachable provider is indistinguishable, at the call site, from a flag that is genuinely off.

## Position within progressive delivery

Flags are the runtime half of progressive delivery; deployment controllers such as Argo Rollouts are the deployment half. The division is that a rollout controller shifts *traffic* between pod versions at the infrastructure layer, whereas a flag gates *behaviour* for a specific subject regardless of which pod serves the request. OpenFeature keeps the behaviour-gating layer from becoming a vendor dependency compiled into every service. An adoption path starts with the open-source `flagd` provider — a daemon reading flag definitions from a file or ConfigMap — and moves to a commercial backend by changing the provider registration.

The boundary of the guarantee: OpenFeature standardises *evaluation*, not *management*. Flag creation, targeting-rule definition and the analytics interface remain properties of the chosen backend and are not portable. What the specification provides is that application code never names the backend.

## Pitfalls

- **A default value chosen for convenience becomes the production configuration during an outage.** Because failures return the default rather than raising, `true` as a default means an unreachable flag service silently enables the new path for everyone.
- **Provider registration is global and startup-ordered.** Evaluations executed before `SetProvider` completes resolve against the no-op default provider, so flags read during static initialisation or early health checks return defaults rather than configured variants.
- **A missing targeting key degrades percentage rollouts to something arbitrary.** Backends bucket subjects by hashing the targeting key; an absent or non-stable key means the same user can land on different sides of a 1% rollout across requests.
- **Hooks execute on the request path.** A `before` hook performing a network call, or an `after` hook exporting telemetry synchronously, adds its latency to every flag evaluation, and evaluations are typically numerous per request.
- **Portability of code is not portability of configuration.** Switching providers moves the call sites unchanged but leaves the targeting rules, segments and flag definitions to be recreated in the new backend's own model.
- **Flags left in place outlive their purpose.** A release toggle that is never removed becomes a permanent branch, and the number of reachable code paths grows as the product of the surviving toggles.
