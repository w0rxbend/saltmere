---
title: "The Anti-Corruption Layer: translating at the boundary, not leaking it"
date: 2026-07-31
track: microservices
summary: "Why you translate another context's model at the edge of your service instead of letting it seep into your domain. Where an ACL sits versus a BFF, a concrete translator sketch, and how to know when to delete it."
reading_time: 5
tags: [microservices, ddd, bounded-context, anti-corruption-layer, integration, scala, migration]
sources:
  - title: "Anti-Corruption Layer pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/anti-corruption-layer"
  - title: "Pattern: Anti-corruption layer — microservices.io (Chris Richardson)"
    url: "https://microservices.io/patterns/refactoring/anti-corruption-layer.html"
  - title: "DDD Strategic Patterns — The Open Group Agile Architecture Standard"
    url: "https://pubs.opengroup.org/architecture/o-aa-standard/DDD-strategic-patterns.html"
  - title: "Building Microservices, 2nd Edition — Sam Newman (O'Reilly, 2021)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Open Host Service — arc42 Quality Model"
    url: "https://quality.arc42.org/approaches/open-host-service"
---

Every integration you build imports someone else's model whether you meant to or not. Call a legacy billing SOAP endpoint, deserialize its `CustAcctRec` with its `statusCd` of `"A" | "S" | "T"`, pass that object one layer inward, and you have just made another team's twenty-year-old vocabulary part of *your* domain. The **Anti-Corruption Layer** (ACL) is the deliberate refusal to do that.

Eric Evans named it in the 2003 blue book as a strategic-design pattern; it sits in the **context map** as a *downstream* defensive relationship — what you reach for when a cooperative relationship (shared kernel, customer/supplier) isn't on the table and you can't change the upstream. Sam Newman gives it a full section in *Building Microservices* 2nd ed. (O'Reilly, 2021), specifically as the thing that stops a monolith's model from bleeding into a new service during a strangler-fig migration.

## What "corruption" actually means

It is not about bad data. Corruption is when the concepts, invariants, and language of a foreign **bounded context** start dictating the shape of your own code. Symptoms:

- Your domain objects carry fields that only make sense to the other system (`legacyPartnerId`, `sapWbsElement`).
- Your business logic branches on *their* status codes.
- A change to their API forces edits deep inside your aggregates, not just at the edge.

The ACL is a translation boundary that keeps their model on their side of a wall. Inside the wall you speak only your ubiquitous language; the layer maps in both directions.

## Adapter, facade, translator — the three internals

The layer is usually described as three collaborating pieces, and it's worth keeping them distinct:

- **Adapter** — deals with the *mechanics*: HTTP/SOAP client, retries, auth, deserialization into the foreign DTO. It knows nothing about your domain.
- **Translator** — the heart of it: pure mapping between the foreign DTO and your domain type. No I/O, no side effects, trivially unit-testable.
- **Facade** — the interface *your* code sees, expressed entirely in your terms (`AccountRepository.find(id): Account`). Callers never learn there's a legacy system behind it.

Keep business rules and orchestration *out* of the layer — Microsoft's guidance is explicit that the ACL is translation only. The moment you put a workflow in there it stops being a wall and becomes a second domain.

## A translator sketch

Foreign model on the left, your domain on the right. The translator is a pure function — that's the property that makes an ACL cheap to trust.

```scala
// --- their model (from the legacy WSDL, do not let this escape the package) ---
final case class CustAcctRec(acctId: String, statusCd: String, balCents: Long)

// --- your model ---
enum AccountStatus:
  case Active, Suspended, Closed

final case class Account(id: AccountId, status: AccountStatus, balance: Money)

// --- translator: the only place that knows both vocabularies ---
object AccountTranslator:
  def toDomain(r: CustAcctRec): Either[TranslationError, Account] =
    for
      status <- r.statusCd match
        case "A" => Right(AccountStatus.Active)
        case "S" => Right(AccountStatus.Suspended)
        case "T" => Right(AccountStatus.Closed)
        case other => Left(TranslationError.UnknownStatus(other))
    yield Account(AccountId(r.acctId), status, Money.ofCents(r.balCents))

final case class TranslationError(msg: String)
```

Two things earn their keep here. The mapping is **total and explicit** — an unknown `statusCd` becomes a typed error at the boundary, not a surprise `case _ =>` deep in your service. And `CustAcctRec` is package-private; the compiler enforces that no other module can accidentally depend on it. That's the wall, made of types.

The facade wires the adapter and translator together and returns only `Account`:

```scala
class LegacyAccountRepository(client: LegacySoapClient):
  def find(id: AccountId): IO[Account] =
    client.fetchAcct(id.value)             // adapter: I/O, retries, auth
      .map(AccountTranslator.toDomain)     // translator: pure
      .flatMap(IO.fromEither)              // fail loudly at the edge
```

## ACL vs BFF — different axes, not competitors

They get conflated because both are "translation layers", but they translate along different axes:

- An **ACL** protects *your model from an upstream's model*. It's inbound, defensive, and lives on the consumer side of a context boundary. Its dual on the provider side is the **Open Host Service** plus a **Published Language** — the upstream offering a clean, documented protocol so *fewer* consumers need their own ACL.
- A **BFF** (backend-for-frontend) adapts *your* model outward to the needs of a specific client (mobile, web). It's about presentation and aggregation, not about defending a domain.

You can have both: a BFF that internally calls a service which itself sits behind an ACL over a legacy core. They compose; they don't overlap.

## When to retire it

The trap is treating a migration-era ACL as permanent furniture. Decide up front which kind you built:

- **Temporary** — it exists only to bridge to a system you are actively strangling. When the last legacy call site is gone, *delete the layer*. Leaving it in place adds latency and a component to maintain for zero benefit, which is exactly the "don't use it when the semantics already match" case in the Azure guidance.
- **Permanent** — the foreign system is here forever (a vendor SaaS, a mainframe of record). Then invest in it: monitoring with correlation IDs, circuit breakers and bulkheads so the translator can't become a shared point of failure.

A cheap tell that you can retire it: the translator has quietly become an identity mapping because the upstream now speaks your language. That's a delete, not a refactor.

**Try next:** Pick one integration in your codebase where a foreign DTO is passed more than one call deep. Write a pure `toDomain` translator for it, make the foreign type package-private, and watch which call sites the compiler now rejects — that list is the corruption you'd been carrying.
