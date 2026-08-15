---
title: "API Composition: Answering Cross-Service Queries Without a Shared Database"
date: 2026-08-15
track: microservices
summary: "In a monolith, a query spanning customers and orders is one SQL JOIN. Once those tables live in services with private databases, that JOIN has no home. API Composition answers the query anyway by calling each provider and joining in memory: the least machinery of the available options, and the lowest ceiling. This article states where the ceiling is."
reading_time: 7
tags: [api-composition, cqrs, database-per-service, queries, aggregation]
sources:
  - title: "Chris Richardson — Pattern: API Composition (microservices.io)"
    url: "https://microservices.io/patterns/data/api-composition.html"
  - title: "Chris Richardson — Pattern: CQRS (microservices.io)"
    url: "https://microservices.io/patterns/data/cqrs.html"
  - title: "API composition pattern — AWS Prescriptive Guidance"
    url: "https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-data-persistence/api-composition.html"
  - title: "Microservices Patterns (Ch. 7, Implementing queries) — Chris Richardson"
    url: "https://microservices.io/book"
  - title: "API Composition Pattern — Joud W. Awad"
    url: "https://joudwawad.medium.com/api-composition-pattern-how-senior-engineers-design-clean-microservices-apis-1ebaa0482991"
---

**Gist.** Under the Database per Service pattern each service owns its store, so a query spanning customers, orders and support tickets has no single schema to `JOIN` against. API Composition executes the query at read time: an API composer calls each owning service and joins the results in application memory. The cost is that the join loses everything a database gives it — indexes, a global sort order, pagination, and a consistent snapshot — and the composite's availability is the product of its providers' availabilities.

## The pattern: an API composer that joins in memory

In a monolith, "each customer with their order count and support-ticket total" is one SQL statement across three tables in one database. Apply **Database per Service** — each service owns its data and no other service touches its store — and the customer rows, order rows and ticket rows sit in three separate databases behind three service application programming interfaces (APIs). No shared schema remains.

Chris Richardson documents two responses on microservices.io. **API Composition** answers the query at read time by calling each service and stitching the results in memory. **Command Query Responsibility Segregation (CQRS)** answers it from a precomputed view fed by events. This article treats the first, whose documented drawbacks are precisely what push a query toward the second.

The pattern has one moving part. An **API composer** (Richardson's term; AWS Prescriptive Guidance calls it an aggregator) implements the query by invoking the **provider services** that own the data and performing an **in-memory join** of the responses. The composer may be the API gateway, a standalone service, or a client-side component. Its behaviour is: fan out, collect, merge, return.

The load-bearing design choice is that provider calls are issued **concurrently rather than sequentially**. Under sequential calls the composer's latency is the sum of the provider latencies; under a concurrent fan-out it is the slowest single provider latency plus merge time, and can be no lower than that maximum. That bound is the reason the fan-out shape is worth the complexity of concurrency.

The second load-bearing choice concerns partial failure. **Not every provider is required for an answer.** A provider supplying the identity of the entity being assembled is essential — without it there is no record to attach anything to. A provider supplying an auxiliary count is not: its absence can be reported as *unknown* rather than substituted with zero. Substituting zero silently converts a failed call into a false fact, and downstream consumers cannot distinguish the two.

### Implementation sketch (Scala)

Composer for a single customer overview: concurrent fan-out, a required provider, and one optional provider that degrades to `None`.

```scala
import scala.concurrent.{ExecutionContext, Future}

final case class Overview(
    customerId: String,
    name: String,
    orderCount: Int,
    lifetimeCents: Long,
    openTickets: Option[Int], // None means "not known", never "zero"
    partial: Boolean
)

def overview(id: String)(using ExecutionContext): Future[Overview] =
  // fan out first, then await: constructing all three Futures before the
  // first flatMap is what makes the calls concurrent rather than chained
  val customerF = customerService.byId(id)
  val ordersF   = orderService.forCustomer(id)
  val ticketsF  = supportService.openTickets(id).map(Option(_))
                    .recover { case _ => None } // optional provider

  for
    customer <- customerF // required: a failure here propagates
    orders   <- ordersF
    tickets  <- ticketsF
  yield Overview(
    customerId    = id,
    name          = customer.name,
    orderCount    = orders.size,                       // the join, keyed on id
    lifetimeCents = orders.map(_.totalCents).sum,
    openTickets   = tickets,
    partial       = tickets.isEmpty
  )
```

Enriching a *list* requires the batched shape instead: collect the foreign keys from the page, issue one call per provider for the whole page, and index the responses before merging.

```scala
def enrich(page: Seq[Order])(using ExecutionContext): Future[Seq[(Order, Customer)]] =
  val ids = page.map(_.customerId).distinct       // one call, not one per row
  customerService.byIds(ids).map { customers =>
    val byId = customers.map(c => c.id -> c).toMap // O(1) probe per order
    page.flatMap(o => byId.get(o.customerId).map(o -> _))
  }
```

## Where the pattern stops

- **In-memory joins of large datasets.** The composer transfers whole result sets over the network and joins them in application heap. Richardson lists the drawback directly: some queries "result in inefficient, in-memory joins of large datasets." A join the database would satisfy through an index becomes a materialise-and-scan in the composer's process. A customer with a dozen orders is unproblematic; "all orders in the last year joined to their line items" is not.
- **N+1 fan-out.** A query returning a list, where each element needs data from another service, invites one call for the list plus one call per element. Request count then grows linearly with page size. The remedy is a **batch endpoint** on the provider — collect the foreign keys, issue a single call for all of them, and join. Where a provider exposes no batch API, composition against it does not scale.
- **Availability multiplies downward.** Each additional provider is another way for the query to fail. AWS Prescriptive Guidance lists reduced availability as a drawback of the pattern: the composite depends on every provider it calls. Where the failures are independent, the availabilities multiply — five providers at 99.9% called synchronously give roughly 99.5% for the composite. Timeouts and degradation to partial answers recover some of that, but **a synchronous fan-out cannot be more available than its dependencies** unless it is permitted to return without them.
- **Pagination and sorting across services.** "The twenty most recent orders across all customers, sorted by date" cannot be paginated by any single provider, because the global sort order exists in no single store. The composer must over-fetch from every provider and sort in memory — the large-in-memory-join problem again, repeated on every page.
- **No cross-service snapshot.** Providers are read at different instants, so a response can combine a customer record read after an order that the order service has not yet acknowledged. The result is a best-effort mosaic, not a transactionally consistent view.

## API composition versus CQRS

When a query outgrows composition, the alternative is to precompute it. **CQRS** maintains a denormalised read model — a materialised view — kept current by events published by the provider services. The join happens **once at write time**, into a store shaped for that query, so the read becomes a single indexed lookup with genuine pagination.

| | API Composition | CQRS materialised view |
|---|---|---|
| When the join happens | At read time, in the composer's memory | At write time, into a denormalised store |
| Extra infrastructure | None beyond the provider calls | A read store plus an event pipeline |
| Large datasets, arbitrary sort, pagination | Poor: over-fetch and sort in memory | Good: one indexed query |
| Availability | Falls as providers are added | Read store serves even while a provider is down |
| Consistency | Best-effort, no snapshot | Eventually consistent (replication lag) |
| Cost to build | Low | Higher: a view to build and keep rebuildable |

Richardson's guidance in *Microservices Patterns* (Ch. 7) is to reach for **API composition first**, since it introduces the least machinery and is frequently sufficient, and to move to **CQRS** when the query needs large joins, cross-service sorting and pagination, or an availability profile a synchronous fan-out cannot deliver. The drawbacks above are the signal that a query has crossed that line. The [CQRS article](/articles/microservices/2026-08-10-cqrs-read-models/) covers the read model and its own trade-off: staleness, and the cost of keeping a projection healthy.

## Pitfalls

- **Sequential `flatMap` chains in the composer.** Latency equals the sum of provider latencies rather than the maximum, because each call is constructed only after the previous one completes. Cause: building the second request inside the first's continuation.
- **Substituting zero for a failed optional provider.** Consumers read a confident "0 open tickets" for a customer with open tickets. Cause: an exception handler that maps failure onto an empty collection instead of onto an explicit unknown.
- **Per-row enrichment on list endpoints.** Provider load and response time grow linearly with page size, so raising the page limit degrades the endpoint. Cause: one enrichment call per element instead of one batched call per page.
- **No per-provider timeout.** One slow provider holds the composite response open, and callers' connections accumulate. Cause: the composer inherits a client default longer than its own response budget.
- **Requesting global sort or deep pagination from the composer.** Memory and transfer grow with the total result set rather than the page, because ordering can only be established after every provider's rows are collected. Cause: no single provider holds the global order.
- **Assuming a consistent view across providers.** Two fields of the same response disagree, for example an order that the customer's aggregate counters do not yet reflect. Cause: reads are taken at different instants with no cross-service snapshot.
