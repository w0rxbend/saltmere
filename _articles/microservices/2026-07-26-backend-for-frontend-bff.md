---
title: "Backends for frontends: one API was never going to fit all"
date: 2026-07-26
track: microservices
summary: "Why a single general-purpose API buckles under web, mobile, and third-party clients, and how a dedicated backend per user experience fixes chatty round-trips and over-fetching. With a working FastAPI aggregating BFF."
reading_time: 5
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

You extracted your monolith into microservices, and it worked. Then you put one general-purpose API server in front of them so clients wouldn't have to know about the topology. That server is now the problem.

Every client team pushes changes into the same deployable. The mobile app makes six round-trips to render one screen because the API returns fine-grained resources. The web app over-fetches fat payloads it throws half away. As Sam Newman puts it, "the single API backend can become a bottleneck when rolling out new delivery, as so many changes are trying to be made to the same deployable artifact." The Backend for Frontend (BFF) pattern is the answer that SoundCloud landed on in 2013, and it is one of the cleaner ideas in *Building Microservices* (2nd ed.).

## The problem with one API to rule them all

A single general-purpose API has to serve clients with genuinely divergent needs:

| Client | Wants | Pays for a shared API with |
| --- | --- | --- |
| Mobile | Few large responses, embedded entities, small over-the-wire size | Chatty round-trips on high-latency networks |
| Web | Fine-grained resources, richer payloads | Over-fetching, or compromise responses |
| Third-party | Stable, general, well-documented contract | Slow evolution to protect external consumers |

You cannot optimize one payload for all three. So the API becomes a negotiated compromise, and every client team queues behind the same release train. SoundCloud hit exactly this: a single Public API serving both their own apps and external integrators, where mobile needed coarse responses to cut requests while web wanted fine-grained ones. The fix was a facade **per user experience** rather than one shared interface.

## The pattern: one experience, one backend

A BFF is a server-side component dedicated to a single frontend. It aggregates and tailors calls to downstream microservices so its one client gets exactly the shape it needs. Newman's guiding rule is "one experience, one BFF" — and it is typically owned by the same team that builds that frontend, which is the organizational payoff: no cross-team negotiation to change your own screen.

The load-bearing job of a BFF is **fan-out aggregation**: turn one client request into several parallel downstream calls and stitch the results into a tailored response. Here is a mobile BFF endpoint in FastAPI that assembles a profile screen from a `users` service and an `orders` service in one round-trip:

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

    # orders are non-critical: degrade gracefully instead of failing
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

The mobile client makes one call over one slow radio link. The BFF makes two fast calls inside the datacenter, drops fields the phone will never render, flattens the entity graph, and degrades gracefully when the non-critical orders service is down. A web BFF can expose the same underlying services with a completely different shape and set of endpoints.

## One BFF per client type, or one per team?

Two variants, both from the field:

- **One per client type** (the REA model): separate BFFs for iOS, Android, and web. Best when the platforms diverge sharply — different navigation, different capabilities, different release cadences. It reduces the risk of any one BFF bloating.
- **One per user experience / team** (the SoundCloud model): iOS and Android share a BFF *if* the experience is genuinely the same. When they diverge greatly, split them.

Newman's advice: default to one experience, one BFF, and only fold two clients together when their needs are close enough that the shared code isn't a constant compromise. SoundCloud went further and split by *use case* — listener apps, creator apps, partner integrations each got their own BFF, enabling independent release cycles and scaling.

## Where API gateways and GraphQL fit

A BFF is not an API gateway. A gateway is a single general-purpose entry point (routing, auth, rate limiting, TLS termination) for *all* traffic — one perimeter component. A BFF is a *specific* backend for *one* frontend, owned by that frontend's team, and it contains client-specific aggregation logic a gateway shouldn't. They compose: a gateway can sit at the edge doing cross-cutting concerns, with BFFs behind it doing the tailoring.

GraphQL is an alternative mechanism for the same goal — letting a client fetch exactly the fields it wants in one round-trip, moving the aggregation into the query rather than a hand-written endpoint. A GraphQL server often *is* the BFF. The trade-off: GraphQL gives clients flexibility for free but makes per-client caching, cost control, and shielding downstream services harder than an explicit endpoint like the one above.

## The pitfalls

Two failure modes dominate:

1. **Logic duplication.** Split BFFs will re-implement the same aggregation and mapping. Newman is pragmatic here: duplicated code across services usually costs less than the coupling you'd reintroduce by sharing it. Reach for a shared library only when the duplication genuinely hurts — and know that shared libraries create upgrade cascades, one of SoundCloud's own hard-won lessons.
2. **The BFF quietly becoming a shared layer.** The moment two clients start pointing at one BFF "to avoid duplication," you have rebuilt the general-purpose API you were escaping — now with worse boundaries. Keep BFFs strictly scoped to their experience. Push shared, genuinely general logic *down* into a downstream service, not sideways into a fattening BFF.

The pattern is deliberately narrow. It solves a coordination and payload-shaping problem, not a business-logic problem. Aggregate and tailor in the BFF; keep the domain rules in the services behind it.

**Try next:** Add a timeout budget to the FastAPI example — give the aggregate call a hard 500 ms deadline with `asyncio.wait_for`, return whatever downstream results arrived before it, and add a `partial: true` flag to the response when the orders service missed the window. Then write a second `/web/profile/{user_id}` endpoint that reuses the same two services but returns a paginated, fully-detailed order list to prove one experience really does get one backend.
