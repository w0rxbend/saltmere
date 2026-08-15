---
title: "Backends for frontends: one API was never going to fit all"
date: 2026-07-26
track: microservices
summary: "Why a single general-purpose API buckles under web, mobile, and third-party clients, and how a dedicated backend per user experience addresses chatty round-trips and over-fetching. With a working FastAPI aggregating BFF."
reading_time: 6
tags: [bff, api-gateway, aggregation, graphql, newman, fastapi]
sources:
  - title: "Sam Newman — Backends For Frontends"
    url: "https://samnewman.io/patterns/architectural/bff/"
  - title: "Service Architecture at SoundCloud — Part 1: Backends for Frontends (SoundCloud Backstage Blog)"
    url: "https://developers.soundcloud.com/blog/service-architecture-1/"
  - title: "BFF @ SoundCloud (Thoughtworks)"
    url: "https://www.thoughtworks.com/en-us/insights/blog/bff-soundcloud"
  - title: "Backends for Frontends pattern (Microsoft Azure Architecture Center)"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/backends-for-frontends"
---

**Gist.** A single general-purpose application programming interface (API) placed in front of a microservice estate must serve clients with incompatible payload requirements, and every client team then queues behind one deployable. The Backend for Frontend (BFF) pattern replaces that shared facade with **one server-side component per user experience**, owned by the team that builds the experience, whose job is fan-out aggregation and payload tailoring. The cost is duplicated aggregation and mapping logic across BFFs, plus one more deployable per client to operate and secure.

## The failure of one shared API

The clients of a shared API differ in what a good response looks like:

| Client | Wants | Pays for a shared API with |
| --- | --- | --- |
| Mobile | Few large responses, embedded entities, small over-the-wire size | Chatty round-trips on high-latency networks |
| Web | Fine-grained resources, richer payloads | Over-fetching, or compromise responses |
| Third-party | Stable, general, well-documented contract | Slow evolution to protect external consumers |

No single payload shape optimises all three at once, so the contract settles into a negotiated compromise. The second cost is organisational rather than technical. Newman states it directly: "the single API backend can become a bottleneck when rolling out new delivery, as so many changes are trying to be made to the same deployable artifact." **The coupling is in the artifact, not in the network topology** — changes from unrelated client teams serialise through one release.

SoundCloud reports exactly this shape: a single Public API served both its own applications and external integrators, with mobile requiring coarse responses to reduce request counts while web wanted fine-grained ones. The pattern that followed was a facade **per user experience** rather than one shared interface.

## The pattern: one experience, one backend

A BFF is a server-side component dedicated to a single frontend. It calls downstream microservices and returns the exact shape that one client renders. Newman's guiding rule is "one experience, one BFF", with ownership sitting in the team that builds that frontend, so a change to one screen requires no cross-team negotiation.

The load-bearing mechanism is **fan-out aggregation**: one inbound request becomes several downstream calls issued in parallel, and the results are stitched into a tailored response. Two properties follow from that. First, response latency is bounded below by the slowest downstream call, not by their sum, provided the calls are genuinely concurrent and independent. Second, the naive availability of the aggregate is the *product* of the downstream availabilities — which is why a BFF must classify each dependency as critical or non-critical and degrade rather than fail on the latter.

A mobile BFF endpoint in FastAPI assembling a profile screen from a `users` service and an `orders` service in one client round-trip:

```python
import asyncio
import httpx
from fastapi import FastAPI, HTTPException

app = FastAPI()
USERS = "http://users-service"
ORDERS = "http://orders-service"

@app.get("/mobile/profile/{user_id}")
async def mobile_profile(user_id: str):
    async with httpx.AsyncClient(timeout=2.0) as client:
        # fan out: both downstream calls in parallel
        user_r, orders_r = await asyncio.gather(
            client.get(f"{USERS}/users/{user_id}"),
            client.get(f"{ORDERS}/orders?user={user_id}&limit=3"),
            return_exceptions=True,
        )

    if isinstance(user_r, Exception) or user_r.status_code != 200:
        raise HTTPException(502, "user service unavailable")
    user = user_r.json()

    # orders are non-critical: degrade instead of failing the screen
    recent = []
    if not isinstance(orders_r, Exception) and orders_r.status_code == 200:
        recent = [
            {"id": o["id"], "total": o["total"], "status": o["status"]}
            for o in orders_r.json()["items"]
        ]

    # tailor: one small payload shaped for the mobile screen
    return {
        "displayName": user["first_name"],
        "avatar": user["avatar_url"],
        "memberSince": user["created_at"][:4],
        "recentOrders": recent,
        "recentOrderCount": len(recent),
    }
```

The client issues one request over one high-latency radio link; the BFF issues two low-latency requests inside the datacentre, drops fields the phone never renders, flattens the entity graph, and returns an empty `recentOrders` list when the non-critical orders service fails. A web BFF may expose the same two services under a completely different endpoint set and response shape.

### Implementation sketch (Scala)

The same fan-out with an explicit **deadline for the whole aggregate**, so a slow non-critical dependency cannot extend the response time of the critical path. The invariant: the endpoint returns no later than the deadline, and marks the response partial when any optional part was dropped.

```scala
import java.util.concurrent.{ScheduledExecutorService, TimeUnit}
import scala.concurrent.{ExecutionContext, Future, Promise}
import scala.concurrent.duration.*

final case class Profile(displayName: String, recentOrders: List[Order], partial: Boolean)

class ProfileBff(users: UserClient, orders: OrderClient, deadline: FiniteDuration)(using
    ec: ExecutionContext,
    scheduler: ScheduledExecutorService
):
  /** `None` when `f` failed or had not completed by the deadline. */
  private def optional[A](f: Future[A]): Future[Option[A]] =
    val timedOut = Promise[Option[A]]()
    scheduler.schedule(
      (() => timedOut.trySuccess(None)): Runnable,
      deadline.toMillis,
      TimeUnit.MILLISECONDS
    )
    Future.firstCompletedOf(Seq(f.map(Some(_)).recover { case _ => None }, timedOut.future))

  def profile(userId: String): Future[Profile] =
    // both calls start before either is awaited: latency is max, not sum
    val userF   = users.byId(userId)                          // critical: failure propagates
    val ordersF = optional(orders.recent(userId, limit = 3))  // optional

    for
      user   <- userF
      recent <- ordersF
    yield Profile(user.firstName, recent.getOrElse(Nil), partial = recent.isEmpty)
```

The `partial` flag is load-bearing, and it is set from the *absence of an answer* rather than from an empty list: without that distinction a client cannot tell "this account has no recent orders" from "the orders service missed the deadline", and will cache the degraded response as if it were the truth.

## One BFF per client type, or one per team

Two variants are reported from the field:

- **One per client type** (the REA model): separate BFFs for iOS, Android and web. This suits platforms that diverge sharply in navigation, capability and release cadence.
- **One per user experience or team** (the SoundCloud model): iOS and Android share a BFF where the experience is genuinely the same, and split where they diverge.

Newman's stated default is one experience, one BFF, folding two clients together only when their needs are close enough that the shared code is not a standing compromise. SoundCloud's account describes the split as following the experiences its teams owned, with external integrators kept behind the general-purpose public API rather than behind an experience-specific BFF.

## Relationship to API gateways and GraphQL

A BFF is not an API gateway. A gateway is a single general-purpose entry point handling cross-cutting concerns — routing, authentication, rate limiting, Transport Layer Security (TLS) termination — for all traffic, and there is one of it at the perimeter. A BFF is a specific backend for one frontend, owned by that frontend's team, holding client-specific aggregation logic that does not belong in a perimeter component. The two compose: gateway at the edge, BFFs behind it.

GraphQL pursues the same goal by a different mechanism, letting a client request exactly the fields it needs in one round-trip and moving aggregation into the query rather than a hand-written endpoint. A GraphQL server frequently *is* the BFF. The trade-off is that client-controlled query shape makes per-client caching, query cost control and shielding of downstream services harder than a fixed endpoint whose fan-out is known in advance.

## Pitfalls

- **Logic duplication across split BFFs.** Each BFF re-implements the same aggregation and field mapping. Newman's position is that duplicated code across services usually costs less than the coupling reintroduced by sharing it, so some duplication is accepted deliberately rather than factored into a library every BFF must upgrade in step.
- **The BFF silently becoming a shared layer.** Pointing a second client at an existing BFF "to avoid duplication" reconstructs the general-purpose API the pattern was meant to escape, now with less distinct boundaries. General logic belongs *downstream* in a service, not sideways in a widening BFF.
- **Domain rules leaking into the BFF.** The pattern addresses coordination and payload shaping, not business logic. Rules placed in a BFF apply to one client only, so a second client silently gets different behaviour.
- **Serial instead of parallel fan-out.** Awaiting each downstream call before starting the next turns the response time into the sum of the dependencies rather than the maximum, and the symptom — latency rising linearly with the number of aggregated services — is invisible in downstream service metrics.
- **Unflagged degraded responses.** A missing optional section rendered as an empty list is indistinguishable from genuinely empty data, and any client-side or intermediary cache will store the degraded body under the same key as a complete one.
