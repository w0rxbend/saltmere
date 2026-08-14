---
title: "API Versioning: URI, Header, and the Expand-Contract Migration"
date: 2026-08-14
track: microservices
summary: "Three ways to version an HTTP API — URI path, custom header, and content negotiation — plus the expand-and-contract (parallel change) pattern that lets you rename a field without breaking a single client or forcing a lockstep release."
reading_time: 6
tags: [api-design, versioning, microservices, backward-compatibility, rest]
sources:
  - title: "Newman, S. — Building Microservices, 2nd Edition (schemas, contracts, avoiding lockstep)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Fowler/Sato — Parallel Change (expand and contract)"
    url: "https://martinfowler.com/bliki/ParallelChange.html"
  - title: "Fowler — Tolerant Reader (Postel's Law for consumers)"
    url: "https://martinfowler.com/bliki/TolerantReader.html"
  - title: "Google API Improvement Proposals — AIP-185: Versioning"
    url: "https://google.aip.dev/185"
---

The moment a service has a consumer you did not write, its API is a contract. Changing that contract carelessly forces a **lockstep release** — client and server must deploy together — which is exactly the coupling microservices exist to avoid. Sam Newman's *Building Microservices* frames the goal plainly: change your service and deploy it *without* having to change or redeploy anyone else. Two things get you there: a versioning strategy for when you truly must break, and the expand-contract pattern so that most of the time you never do.

## Where the version goes

**URI path** — `GET /v2/orders/42`. Explicit, cacheable, trivially routable at the gateway. Google's AIP-185 mandates exactly this: a single *major* version encoded as the first path segment (`v1`, `v2`), with no minor or patch numbers in the URL. Purists dislike that the same resource now has two URLs, but for operability it is hard to beat.

**Custom header / content negotiation** — keep the URL stable and version via `Accept`:

```http
GET /orders/42 HTTP/1.1
Accept: application/vnd.acme.order.v2+json
```

The resource identity stays clean; the representation is negotiated. The cost is that a version is now invisible in logs and browser URLs, and caches must vary on the header.

**Semantic versioning of the contract itself.** Regardless of transport, think in semver terms: additive/backward-compatible changes are minor and need no new endpoint; breaking changes are major and do. The discipline is deciding *which* is which — and that is where most teams get burned.

## Expand and contract: change without breaking

Most changes do not need a `/v2` at all if both sides follow **Postel's Law** — *be conservative in what you send, liberal in what you accept*. Fowler's **Tolerant Reader** says a consumer should read only the fields it needs and ignore everything else, so adding a field is a non-event. That property is what makes the **parallel change** pattern (Sato/Fowler; also called expand-and-contract) work. It has three phases:

1. **Expand** — add the new shape alongside the old; both are populated.
2. **Migrate** — move clients over, one at a time, on their own schedule.
3. **Contract** — once no one reads the old shape, remove it.

Say you must rename `name` to `full_name`. A naive rename breaks every reader instantly. Expand-contract instead does this:

```json
// Phase 1 — EXPAND: write both, keep them in sync
{
  "id": 42,
  "name": "Ada Lovelace",        // old field, still populated
  "full_name": "Ada Lovelace"    // new field, tolerant readers pick this up
}

// Phase 2 — MIGRATE: clients switch to full_name at their own pace.
//           Deprecate the old field explicitly:
//   Deprecation: true
//   Sunset: Wed, 01 Oct 2026 00:00:00 GMT
//   Link: <https://api.acme.dev/docs/order-v2>; rel="deprecation"

// Phase 3 — CONTRACT: after the sunset date, drop "name"
{
  "id": 42,
  "full_name": "Ada Lovelace"
}
```

The server does the double-write in phase 1 — often a one-line mapping in the serializer. Consumers migrate whenever they redeploy for other reasons; nobody is blocked. The `Deprecation` and `Sunset` HTTP headers (RFC 8594) turn "please stop using this" into a machine-readable signal your clients can alarm on, rather than an email nobody reads.

## Making it safe in practice

- **Never remove or repurpose a field in place.** Removing `name` and later reusing the key for something else is the classic silent break. Add new keys; retire old ones only after the sunset window.
- **Verify with consumer-driven contract tests.** Newman leans hard on these: each consumer publishes the shape it expects (e.g. Pact), and the provider's CI fails if a change would violate a real consumer's expectations — you learn at build time, not from a 2 a.m. page.
- **Reserve a new major version for genuinely incompatible changes** — restructuring a resource, removing an operation, changing types. AIP-185 requires old and new majors to run *simultaneously* through a transition period so clients migrate gradually, never in lockstep.

Done well, "versioning" stops being a big-bang event. The expand-contract loop is small and repeatable, and a real `/v2` becomes the rare exception rather than the quarterly ritual.

**Try next:** pick an endpoint and add one field via expand-contract — double-write it, attach `Deprecation`/`Sunset` headers to the old field, and write a Pact-style contract test that fails if the old field disappears before the sunset date.
