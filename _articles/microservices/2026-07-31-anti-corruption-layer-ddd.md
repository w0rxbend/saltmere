---
title: "The Anti-Corruption Layer: translating at the boundary, not leaking it"
date: 2026-07-31
track: microservices
summary: "Translating another context's model at the edge of a service instead of letting it seep into the domain. Where an anti-corruption layer sits relative to a backend-for-frontend, a translator sketch, and the conditions under which it can be deleted."
reading_time: 6
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

**Gist.** An integration imports the model of whatever system it calls: deserializing a legacy billing endpoint's `CustAcctRec`, with its `statusCd` drawn from `"A" | "S" | "T"`, and passing that object inward makes another team's vocabulary part of the consuming domain. The **anti-corruption layer** (ACL) is a translation boundary that maps foreign representations to local ones in both directions, so that only the layer knows both vocabularies. The cost is a component that must be written, tested, deployed and monitored, and an extra mapping step on every call — a cost that survives the reason it was introduced unless retirement is planned.

Eric Evans named the pattern in *Domain-Driven Design* (2003) as a strategic-design pattern. It occupies a position in the **context map** as a *downstream, defensive* relationship: one of the options for a downstream context, alongside cooperative relationships such as shared kernel and customer/supplier, and the one that does not require the upstream to change. Sam Newman treats it in *Building Microservices*, 2nd edition (O'Reilly, 2021) as the mechanism that stops a monolith's model from bleeding into a new service during a strangler-fig migration.

## What corruption denotes

Corruption here is not malformed data. It is the condition in which the concepts, invariants and language of a foreign **bounded context** begin to dictate the shape of local code. Three observable symptoms:

- Domain objects carry fields meaningful only to the other system (`legacyPartnerId`, `sapWbsElement`).
- Local business logic branches on *foreign* status codes, which makes the foreign encoding an invariant of the local model.
- A change to the upstream API forces edits inside aggregates rather than at the edge — the diagnostic property, because it shows the dependency has passed through the boundary.

The ACL keeps the foreign model on the foreign side of a wall. Inside the wall only the local ubiquitous language appears.

## Adapter, translator, facade

The layer is conventionally decomposed into three collaborating pieces, and the separation is load-bearing because each has a different testability profile:

- **Adapter** — the *mechanics*: HTTP or SOAP client, retries, authentication, deserialization into the foreign data transfer object (DTO). It carries no knowledge of the local domain, and it is the only part that performs input/output.
- **Translator** — pure mapping between the foreign DTO and the local domain type. **No I/O and no side effects**, so it is testable by example without a fixture or a network stub.
- **Facade** — the interface the local code sees, expressed entirely in local terms (`AccountRepository.find(id): Account`). Callers do not learn that a legacy system stands behind it.

Business rules and orchestration belong outside the layer. The Azure Architecture Center guidance describes the layer's responsibility as translation between the two systems' models; a workflow placed inside it makes the layer a second domain model, which reintroduces the coupling the wall was meant to prevent.

### Implementation sketch (Scala)

The foreign model appears on one side, the local domain on the other. The translator is a total function into `Either`, which is the property that makes the layer cheap to trust.

```scala
// --- foreign model (from the legacy WSDL; must not escape this package) ---
private[legacy] final case class CustAcctRec(acctId: String, statusCd: String, balCents: Long)

// --- local model ---
enum AccountStatus:
  case Active, Suspended, Closed

final case class Account(id: AccountId, status: AccountStatus, balance: Money)

enum TranslationError:
  case UnknownStatus(code: String)

// --- translator: the only place that knows both vocabularies ---
private[legacy] object AccountTranslator:
  def toDomain(r: CustAcctRec): Either[TranslationError, Account] =
    val status: Either[TranslationError, AccountStatus] = r.statusCd match
      case "A"   => Right(AccountStatus.Active)
      case "S"   => Right(AccountStatus.Suspended)
      case "T"   => Right(AccountStatus.Closed)
      case other => Left(TranslationError.UnknownStatus(other))
    status.map(s => Account(AccountId(r.acctId), s, Money.ofCents(r.balCents)))
```

Two properties earn their keep. The mapping is **total and explicit**: an unrecognised `statusCd` becomes a typed error at the boundary rather than a fall-through `case _ =>` reached deep inside a service, where the offending value's origin is no longer visible in the stack. And `CustAcctRec` is package-private, so the compiler — not a review convention — enforces that no other module depends on it. The wall is made of types.

The facade composes adapter and translator and exposes only `Account`:

```scala
final class LegacyAccountRepository(client: LegacySoapClient):
  def find(id: AccountId): Account =
    val rec = client.fetchAcct(id.value)          // adapter: I/O, retries, auth
    AccountTranslator.toDomain(rec) match         // translator: pure
      case Right(account) => account
      case Left(err)      => throw TranslationFailed(err)
```

The rejection of a `Left` at this line is the enforcement point of the invariant: **no value crosses the facade unless it is a well-formed domain value**. Anything the translator cannot map fails the call there, so downstream code never needs a branch for a state the local model does not define.

## Anti-corruption layer versus backend-for-frontend

The two are conflated because both are described as translation layers, but they translate along different axes.

- An **ACL** protects the local model from an *upstream's* model. It is inbound, defensive, and lives on the consumer side of a context boundary. Its counterpart on the provider side is the **Open Host Service** together with a **Published Language**: the upstream publishes a documented protocol, which reduces the number of consumers that need an ACL of their own.
- A **backend-for-frontend** (BFF) adapts the local model *outward* to the needs of one client class, such as mobile or web. Its concerns are presentation and aggregation, not defence of a domain model.

Both can be present simultaneously — a BFF calling a service that itself sits behind an ACL over a legacy core. They compose along orthogonal axes rather than competing.

## Retirement

Treating a migration-era ACL as permanent furniture is the recurring failure. The two cases differ in what should be invested in the layer:

- **Temporary** — the layer bridges to a system being actively strangled. Once the last legacy call site is removed, the layer is deleted. Retaining it adds a network or mapping hop and a component to maintain with no remaining consumer, which matches the Azure guidance's advice against using the pattern when the semantics on both sides already match.
- **Permanent** — the foreign system is durable (a vendor SaaS product, a mainframe system of record). The layer then warrants operational investment: correlation identifiers through the translation, and circuit breakers and bulkheads so that a single slow upstream cannot exhaust the resources of every caller sharing the layer.

One observable signal for retirement: the translator has degenerated into an identity mapping because the upstream now speaks the local language. That condition indicates deletion rather than refactoring.

A practical starting exercise: locate an integration in which a foreign DTO is passed more than one call deep, write a pure `toDomain` translator for it, make the foreign type package-private, and inspect which call sites the compiler then rejects. That list enumerates the corruption already present.

## Pitfalls

- **A non-total translator hides upstream drift.** A `case _ => Active` default turns a newly introduced upstream status code into silently wrong domain state; the error appears later as a business-rule anomaly with no trace back to the boundary.
- **The foreign DTO is public.** If `CustAcctRec` is not visibility-restricted, a caller imports it for convenience, and the wall exists only by convention; the next upstream field rename then produces a compile error inside an aggregate.
- **Orchestration migrates into the layer.** Once the ACL makes a second call to decide what to translate, it holds business rules, and changes to the local domain now require edits to the integration component.
- **The layer becomes a shared point of failure.** A permanent ACL serving many callers with no bulkhead lets one slow upstream dependency consume the thread or connection budget of unrelated flows.
- **The temporary layer outlives the migration.** After the last legacy call site is removed, an unretired ACL is a maintained, deployed, monitored component with no consumer, and its presence suggests to readers that the legacy system is still live.
- **Both sides mutate the same object.** Returning the foreign DTO wrapped in a local type, rather than constructing a new domain value, leaves foreign fields reachable and reintroduces the dependency the translation was intended to sever.
