---
title: "Strangler Fig: decomposing a monolith one route at a time"
date: 2026-07-30
track: microservices
summary: "You rarely get to rewrite a monolith in one shot — big-bang replacements fail because nobody fully understands the old behavior. The Strangler Fig pattern puts a proxy in front of the monolith and peels off one capability at a time, routing individual endpoints to new services while everything else keeps working. Here's the interceptor, the routing, the canary, and the data problem nobody warns you about."
reading_time: 6
tags: [strangler-fig, monolith, migration, reverse-proxy, feature-flags, cdc]
sources:
  - title: "StranglerFigApplication — Martin Fowler"
    url: "https://martinfowler.com/bliki/StranglerFigApplication.html"
  - title: "Monolith to Microservices — Sam Newman"
    url: "https://samnewman.io/books/monolith-to-microservices/"
  - title: "Strangler Fig pattern — Azure Architecture Center"
    url: "https://learn.microsoft.com/en-us/azure/architecture/patterns/strangler-fig"
  - title: "Strangler fig pattern — AWS Prescriptive Guidance"
    url: "https://docs.aws.amazon.com/prescriptive-guidance/latest/modernization-decomposing-monoliths/strangler-fig.html"
---

Martin Fowler named this pattern after a plant. On a 2001 trip through the Queensland rainforest he watched strangler figs germinate high on a host tree, send roots down around its trunk, and eventually grow into a self-supporting shape while the original tree rotted away inside. That is exactly the migration you want: the new system grows around the edges of the monolith until the monolith is hollow and can be removed — no flag day, no six-month code freeze, no "we'll cut over Saturday night and pray."

The reason to do it this way is not aesthetic. Big-bang rewrites fail because the hard part of a legacy system is the behavior nobody documented, and you only discover those nuances when the rewrite gets them wrong in production. Strangler Fig replaces the system in slices small enough that each slice's blast radius is one capability, and each slice can be rolled back.

## The interceptor is the whole trick

Everything hinges on one component sitting between clients and the monolith: a **reverse proxy** (Fowler and the Azure/AWS write-ups all call it a *facade* or *interception layer*). Clients keep calling the same host. The proxy decides, per request, whether a call goes to the old monolith or a new service. On day one it forwards 100% to the monolith and does nothing else — which is the point, because you want to install and validate the interceptor *before* you've moved any behavior, so the proxy itself is never the risky change.

Once it's in place, migrating a capability becomes a routing edit. Here's NGINX peeling the shipping-quote endpoint off to a new service while everything else stays on the monolith:

```nginx
upstream monolith   { server monolith.internal:8080; }
upstream shipping_v2 { server shipping-svc.internal:9090; }

server {
  listen 80;

  # Peeled off: this one path now goes to the new service
  location = /api/shipping/quote {
    proxy_pass http://shipping_v2;
  }

  # Everything else still hits the monolith
  location / {
    proxy_pass http://monolith;
  }
}
```

That's the pattern in miniature. `location` blocks are your migration ledger: the more specific routes at the top point at new services, the catch-all `/` at the bottom is the shrinking monolith. Match order matters — exact and prefix matches must sit above the fallthrough or your new service never sees traffic. Envoy, HAProxy, an API gateway (AWS recommends API Gateway for exactly this), or a service mesh all do the same job; NGINX is just the smallest thing that shows the shape.

## Route by endpoint, not by guesswork

Pick seams, not features you *wish* were separate. A good first slice is a capability that (a) has a clean HTTP surface you can name with a route, (b) reads and writes a bounded set of data, and (c) is valuable enough that shipping it proves the migration works. Newman's advice in *Monolith to Microservices* is to start with something low-risk and high-learning — you are as much migrating your deployment pipeline and on-call muscle as you are migrating code.

Watch out for the endpoints that *look* independent but aren't. `/api/shipping/quote` is a great candidate if it only needs address and weight. It's a trap if, three calls deep, the monolith's quote logic also decrements inventory and writes to the orders table — now you have two systems fighting over the same rows.

## Roll each capability out behind a toggle

Do not flip a route from monolith to new service in one commit. The interception layer is the natural place to do a **canary**: send 1% of `/api/shipping/quote` to `shipping_v2`, compare results and latency against the monolith, then ramp. Two common mechanisms:

- **Weighted routing** at the proxy — split traffic by percentage. Envoy and most gateways express this as weighted clusters; you dial from 99/1 to 0/100 over days.
- **A feature toggle** the proxy consults per request, so product or ops can shift or kill the new path without a redeploy.

Because the monolith is still running and still correct, rollback is free during this window: set the weight back to 0, or flip the flag off. AWS frames the whole middle phase as *coexist* precisely so this reversion stays cheap. You lose that safety net only after you delete the old code path — so don't delete it until the canary has been at 100% and quiet for a while.

You can even run **dark / shadow traffic** first: mirror requests to `shipping_v2`, throw away its responses, and diff them against the monolith's. That catches the undocumented-behavior bugs before a single real user is affected.

## The data layer is where it gets hard

Routing HTTP is easy. The moment your new service and the monolith both need the same data, the neat picture breaks. Three realities, roughly in order of pain:

| Stage | What the new service does with data | Risk |
|---|---|---|
| Shared DB | Reads/writes the monolith's tables directly | Fast to ship, but you haven't actually decoupled — the schema is still a shared contract |
| CDC sync | Owns a new store; Change Data Capture streams the monolith's writes into it | Eventual consistency; must handle lag and replay |
| System of record | New store is authoritative; monolith reads *from* the service | Cutover of writes is the genuinely scary step |

The usual path (spelled out in the Azure guidance) is: let the new service read/write the shared monolith database at first, extract its tables into a domain-owned store via ETL, keep them in sync with CDC, then promote the new store to system of record and remove the old tables. Shield the new service's model from the monolith's schema with an **anti-corruption layer** so the legacy data model doesn't leak into your clean design and calcify it.

## When NOT to reach for it

Strangler Fig is not free, and a few situations make it the wrong tool:

- **You can't intercept the calls.** The entire pattern assumes a proxy can sit in front and route. If clients talk to the backend through channels you can't put a facade on, there's nothing to strangle.
- **The system is small.** If a straight rewrite-and-replace is genuinely achievable in one go, the transitional proxy, dual data paths, and CDC plumbing are overhead you don't need. Fowler's own warning: this is for the systems too big or too risky to replace wholesale.
- **You need the old system gone *now*.** The strangler is deliberately slow; coexistence can run for months. If a compliance deadline or license cliff forces a fast decommission, this pattern fights you.
- **You lack source access to the monolith.** You often need to disable the migrated feature inside the old code and redirect its internal calls; a black-box binary you can't change limits how completely you can strangle it.

And don't forget the interceptor is now on every request's path: it's a single point of failure and a latency tax. Keep it dumb, horizontally scaled, and boring — its whole value is being the one component you trust while everything behind it churns.

**Try next:** Put NGINX (forwarding 100% to your monolith) in front of one real service in staging, change nothing else, and prove no behavior shifted. Then pick one read-only endpoint, stand up a trivial replacement, and mirror shadow traffic to it — diff the two responses before you route a single user there.
