---
title: "API Composition: Answering Cross-Service Queries Without a Shared Database"
date: 2026-08-15
track: microservices
summary: "In a monolith, a query spanning customers and orders is one SQL JOIN. Split those into services with their own databases and that JOIN is illegal — the tables live in different stores you don't own. API Composition answers the query anyway by calling each provider and joining in memory. It's the simplest option and the one with the lowest ceiling; here's exactly where it stops scaling."
reading_time: 6
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

In a monolith, "show me each customer with their order count and support-ticket total" is one SQL statement: a `JOIN` across three tables in one database. Apply the **Database per Service** pattern — each service owns its data, no other service touches its store — and that query has no home. The customer rows, order rows, and ticket rows now live in three separate databases behind three service APIs. There is no shared schema to join against.

Chris Richardson names two ways out on microservices.io. **API Composition** answers the query at read time by calling each service and stitching the results in memory. **CQRS** answers it with a precomputed view fed by events. This article is about the first — the simpler of the two, and the one whose limits you should know before reaching for it, because those limits are exactly what push teams toward the second.

## The pattern: an API composer that joins in memory

The pattern has one moving part. An **API Composer** (Richardson's term; AWS calls it an aggregator) implements the query by invoking the **provider services** that own the data and performing an **in-memory join** of what comes back. The composer is often the API gateway, but it can be a standalone service or a client-facing endpoint. Its whole job is: fan out, collect, merge, return.

The critical design choice is calling the providers **in parallel**, not in sequence — the composer's latency is a fan-out, so it should be bounded by the slowest single provider, not the sum of all of them. Here is a composer that assembles a customer overview from three independent services:

```python
import asyncio
import httpx

CUSTOMERS = "http://customer-service"
ORDERS    = "http://order-service"
TICKETS   = "http://support-service"

async def customer_overview(customer_id: str) -> dict:
    async with httpx.AsyncClient(timeout=2.0) as c:
        # fan out: three provider calls concurrently, not one after another
        cust, orders, tickets = await asyncio.gather(
            c.get(f"{CUSTOMERS}/customers/{customer_id}"),
            c.get(f"{ORDERS}/orders?customer={customer_id}"),
            c.get(f"{TICKETS}/tickets?customer={customer_id}&state=open"),
            return_exceptions=True,   # one provider failing must not kill the batch
        )

    # the identity provider is required; without it there is no answer to return
    if isinstance(cust, Exception) or cust.status_code != 200:
        raise RuntimeError("customer service unavailable")
    result = cust.json()

    # the join: match provider results back to the customer, in memory
    result["order_count"] = _count_ok(orders, "orders")
    result["lifetime_cents"] = _sum_ok(orders, "orders", "total_cents")

    # tickets are non-critical — degrade to a partial answer instead of failing
    if isinstance(tickets, Exception) or tickets.status_code != 200:
        result["open_tickets"] = None      # signal "unknown", not "zero"
        result["partial"] = True
    else:
        result["open_tickets"] = len(tickets.json()["tickets"])

    return result
```

That is the entire pattern: three concurrent calls, a join keyed on `customer_id`, and an honest partial result when a non-essential provider is down. For a single entity assembled from a handful of services, it is hard to beat for simplicity — no extra datastore, no event plumbing, no eventual consistency to reason about. Richardson's own summary is that it gives you "a simple way to query data in a microservice architecture." The word doing the work there is *simple*.

## Where it breaks down

The simplicity has a ceiling, and you hit it fast once the query stops being "one entity by ID."

- **In-memory joins of large datasets.** The composer pulls whole result sets over the network and joins them in application memory. Richardson lists the drawback plainly: some queries "result in inefficient, in-memory joins of large datasets." A join a database does with an index becomes a full materialize-and-scan in your service heap. Fine for a customer and their 12 orders; a disaster for "all orders in the last year joined to their line items."
- **N+1 fan-out.** The nastiest failure mode. A query that returns a *list* — say 50 orders — where each row needs data from another service tempts you into 1 call for the list plus N calls to enrich each element. That is 51 requests, and it degrades linearly with page size. The fix is a **batch endpoint**: fetch the 50 order rows, collect their customer IDs, and make *one* `GET /customers?ids=a,b,c` call, then join. If a provider offers no batch API, API composition against it will not scale, full stop.
- **Availability math.** Every provider is a new way for the query to fail. AWS states it directly: overall availability *decreases* as the number of services behind the composer grows, because you multiply their individual availabilities. Five 99.9% providers called synchronously give roughly 99.5% for the composite. Timeouts and graceful degradation claw some back, but you can't make a synchronous fan-out more available than its dependencies.
- **Pagination and sorting across services.** The hard wall. "The 20 most recent orders across all customers, sorted by date" can't be paginated by any single provider, because the global sort order lives in no one store. The composer would have to over-fetch from every provider and sort in memory — collapsing straight back into the large in-memory-join problem, now on every page.
- **Consistency.** Providers are queried at slightly different instants, so the result can reflect a customer read *after* an order the order service hasn't acknowledged. There is no cross-service snapshot — a best-effort mosaic, not a transactionally consistent view.

## API composition vs CQRS

When the query outgrows composition, the alternative is to precompute it. **CQRS** maintains a denormalized read model — a materialized view — that provider services keep current by publishing events; the join happens *once at write time* into a store shaped for exactly this query, so the read is a single indexed lookup with real pagination.

| | API Composition | CQRS materialized view |
|---|---|---|
| When the join happens | At read time, in the composer's memory | At write time, into a denormalized store |
| Extra infrastructure | None — just calls the providers | A read store + event pipeline to feed it |
| Large datasets / arbitrary sort / pagination | Poor — over-fetch and sort in memory | Good — it's one indexed query |
| Availability | Falls with each provider added | Read store stays up even if a provider is down |
| Consistency | Best-effort, no snapshot | Eventually consistent (replication lag) |
| Cost to build | Low | Higher — a whole view to build and keep rebuildable |

The honest rule of thumb from Richardson's *Microservices Patterns* (Ch. 7): reach for **API composition first** — it is the least machinery and often enough. Move to **CQRS** only when the query genuinely needs large joins, cross-service sorting and pagination, or an availability profile that a synchronous fan-out can't deliver. Composition's drawbacks aren't bugs to engineer around; they're the signal that the query has outgrown the pattern. (This site's [CQRS article](/articles/microservices/2026-08-10-cqrs-read-models/) covers the read model and its own trade-off: staleness and the cost of keeping a projection healthy.)

**Try next:** Take the composer above and give it a list endpoint — `GET /overviews?ids=a,b,c` — implemented naively as one enrichment call per id. Watch the request count grow with the page size, then collapse it into a single batched `GET /customers?ids=...` call and join in memory. That refactor, from N+1 to one batch call, is the difference between API composition scaling and falling over.
