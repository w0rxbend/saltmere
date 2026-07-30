---
title: "OpenFeature: feature flags without marrying a vendor"
date: 2026-07-30
track: microservices
summary: "Feature-flag SDKs are sticky — write GetFlag(...) against LaunchDarkly everywhere and switching costs a rewrite. OpenFeature is a CNCF vendor-neutral flag API: your code calls a standard client, and a swappable provider talks to whatever backend you use. Here's the model, the Go/Java shape, and where hooks fit."
reading_time: 5
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

Feature flags are how you decouple *deploy* from *release*: ship the code dark, then turn it on for 1% of users, then 50%, then everyone — or kill it instantly if it misbehaves. The problem isn't the concept, it's the lock-in. The day you write `launchdarkly.BoolVariation("new-checkout", ...)` in two hundred call sites, you've married that vendor. Switching — or running different backends in different environments — means touching every one.

OpenFeature, a **CNCF incubating project since December 2023**, is the standardization play. It's a vendor-neutral *API specification* plus SDKs for most languages. Your application code depends only on the OpenFeature interface; a **provider** — a thin adapter — connects that interface to an actual backend (LaunchDarkly, Flagsmith, Unleash, GO Feature Flag, flagd, or a homegrown service). Swapping backends becomes a one-line provider change instead of a rewrite.

## The three moving parts

**Provider** — the adapter that knows how to talk to your flag backend. You set it once, at startup, globally.

**Client** — what your code evaluates flags against. It's backend-agnostic; it just asks the currently-registered provider.

**Evaluation context** — the "who/what" of this evaluation: user ID, plan tier, region, whatever your targeting rules key on. Passed per evaluation.

Here's the whole shape in Go:

```go
import (
    "context"
    "github.com/open-feature/go-sdk/openfeature"
)

func main() {
    // 1. Register a provider ONCE at startup (swap this line to change backends).
    openfeature.SetProvider(flagd.NewProvider())

    // 2. Get a client anywhere in your app.
    client := openfeature.NewClient("checkout-service")

    // 3. Build context for THIS user, then evaluate with a safe default.
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

Two things are worth calling out. First, **every evaluation takes a default value** (`false` above). If the provider is unreachable or the flag is missing, you get the default, not an exception — flags fail safe by design. Second, the *only* vendor-specific line is `SetProvider`. Everything else is portable across every OpenFeature backend. The Java, Node, Python, and .NET SDKs mirror this exactly (`client.getBooleanValue("new-checkout", false, ctx)` in Java).

## Hooks: the cross-cutting layer

Hooks are OpenFeature's extension points that fire around each evaluation — `before`, `after`, `error`, `finally`. This is where you wire flags into the rest of your platform without touching call sites:

- Emit an OpenTelemetry span or metric for every flag evaluated (so you can answer "which flag caused this behavior change?").
- Enrich the evaluation context automatically (inject the trace ID or authenticated user).
- Log or validate.

Because hooks are registered on the client or provider, one hook covers every evaluation in the service. That's the piece that makes OpenFeature more than a lowest-common-denominator wrapper — the telemetry and context plumbing is standardized too.

## Where it fits with progressive delivery

Flags are the *runtime* half of progressive delivery; tools like Argo Rollouts (covered earlier here) are the *deployment* half. A clean division: Rollouts shifts *traffic* to a new pod version at the infrastructure layer; flags gate *behavior* for specific users regardless of which pod they hit. OpenFeature keeps the behavior-gating layer from becoming a vendor dependency baked into every service. You can even start with the open-source `flagd` provider (a small daemon reading flag definitions from a file or ConfigMap) and migrate to a commercial backend later by changing exactly one line.

The honest caveat: OpenFeature standardizes *evaluation*, not *management*. Creating flags, defining targeting rules, and the analytics UI still live in whatever backend you pick — those aren't portable. What OpenFeature buys you is that your *code* never learns which backend that is.

**Try next:** Add the OpenFeature SDK to one service with the `flagd` provider reading a local JSON flag file, gate one code path behind a boolean flag, then swap `SetProvider` to a second provider (or Unleash) and confirm not a single call site changed — that unchanged diff everywhere except one line is the entire value proposition.
