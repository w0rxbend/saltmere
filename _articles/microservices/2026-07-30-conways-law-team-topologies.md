---
title: "Conway's Law is not a warning, it's a design tool: the Inverse Conway Maneuver in practice"
date: 2026-07-30
track: microservices
summary: "Your service boundaries will mirror your org chart whether you plan for it or not. Here's a worked example of how a functionally-siloed org produces layered, wrong-grained services, and how re-orging into stream-aligned teams (Team Topologies) fixes the boundaries at the source."
reading_time: 6
tags: [conways-law, team-topologies, org-design, service-boundaries, stream-aligned-teams, microservices]
sources:
  - title: "How Do Committees Invent? — Melvin E. Conway, Datamation, April 1968 (melconway.com)"
    url: "https://www.melconway.com/Home/Committees_Paper.html"
  - title: "Team Topologies — Key Concepts (Skelton & Pais)"
    url: "https://teamtopologies.com/key-concepts"
  - title: "Building Microservices, 2nd ed. — Sam Newman (organizational structures)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
  - title: "Inverse Conway Maneuver — Thoughtworks Technology Radar"
    url: "https://www.thoughtworks.com/radar/techniques/inverse-conway-maneuver"
  - title: "Visualize Team Dependencies with a Team API — IT Revolution"
    url: "https://itrevolution.com/articles/visualize-team-dependencies-with-a-team-api/"
---

In 1968, Melvin Conway published a paper in *Datamation* with a thesis buried near the end:

> "organizations which design systems (in the broad sense used here) are constrained to produce designs which are copies of the communication structures of these organizations."

Read that as an engineering constraint, not a proverb. It says the shape of your software is *downstream* of who talks to whom. Newman's organizational-structures chapter in *Building Microservices* leans on exactly this: you cannot draw clean service boundaries on a whiteboard and expect them to survive contact with an org that's structured a different way. The org wins. Every time.

So the useful question isn't "how do I fight Conway's Law?" It's "how do I aim it?"

## A worked example: three siloed teams, three wrong services

Say you're breaking up a monolithic e-commerce app. Your engineering org is structured by *technical function* — the way a lot of shops still are:

- a **DBA / data team** that owns all schemas and stored procedures,
- a **middleware / backend team** that owns the application servers and business logic,
- a **frontend team** that owns the web and mobile UI.

Each team communicates well *internally* and hands off *across* the boundary via tickets. Conway's Law predicts the architecture those communication paths will produce, and it's depressingly precise:

```
Client ─▶ [ Frontend Service ]   ← frontend team
              │  (calls down via REST)
              ▼
        [ Business Logic Service ]  ← middleware team
              │  (calls down via more REST)
              ▼
        [ Data Access Service ]     ← DBA team
              │
              ▼
           one shared DB
```

You didn't get microservices. You got a **layered monolith with network calls between the layers** — the worst of both worlds. Look at what happens when a product manager asks for a change to *checkout*:

- The frontend team changes the checkout screen.
- The middleware team changes checkout logic.
- The DBA team adds a column and a stored proc.

One business feature, three teams, three backlogs, three deploys, and a release train to sequence them. Latency is now stacked across three network hops that used to be in-process calls. Nobody owns "checkout" — they own *a horizontal slice of everything*. The boundaries are real; they're just cut along the wrong axis. They copy the org chart, exactly as Conway said they would.

## The Inverse Conway Maneuver

Thoughtworks named the fix on their Technology Radar: the **Inverse Conway Maneuver** — *"evolving your team and organizational structure to promote your desired architecture."* Instead of accepting the org as fixed and grieving the architecture it forces, you change the org so its communication structure matches the boundaries you actually want.

You want a **checkout** service, a **catalog** service, a **payments** service — vertical slices, each a business capability, each owned end-to-end by one team. So you build teams shaped like that.

## Team Topologies: the shapes you re-org *into*

Skelton and Pais give the Inverse Conway Maneuver a concrete target. Don't just say "cross-functional teams"; pick from four fundamental team types and wire them with three defined interaction modes.

| Team type | Owns | In our example |
|---|---|---|
| **Stream-aligned** | A single value stream / business capability, end-to-end | `checkout`, `catalog`, `payments` teams — each with front-to-back skills |
| **Platform** | Internal self-service products that reduce load on stream teams | CI/CD, k8s, observability, the shared DB-as-a-service |
| **Enabling** | Coaching stream teams over a capability gap, then leaving | An ex-DBA group teaching teams to own their own schemas |
| **Complicated-subsystem** | A part needing deep specialist knowledge | A pricing/fraud engine too gnarly for every stream team to hold |

The **stream-aligned team is the default and the majority** — the DBA and middleware silos dissolve *into* the stream teams (or become a platform/enabling team). Now the checkout team has a UI dev, a service dev, and someone who knows the data model, and it ships checkout without a cross-team ticket.

The three **interaction modes** keep the wiring explicit:

- **Collaboration** — two teams work closely for a short time to figure out new terrain (high bandwidth, high cost — use sparingly).
- **X-as-a-Service** — one team consumes another with minimal fuss (the checkout team just *uses* the platform's database-as-a-service; no meetings).
- **Facilitation** — an enabling team helps another remove an impediment.

The whole point of Team Topologies is managing **cognitive load**: overloaded teams make poor decisions and move slowly. A stream-aligned team owning one capability with a good platform underneath can hold its whole domain in its head. The old middleware team, owning a horizontal slice of *twelve* domains, never could.

## Make the boundary legible: a team API

A re-org only produces good architecture if the new boundaries are *explicit*. Team Topologies suggests each team publish a **team API** — the interface other teams integrate against. Keep it in the repo, version it, keep it honest:

```markdown
# Team API — Checkout

Team type:   Stream-aligned
Mission:     Own the checkout journey, cart → confirmed order.

## Owns
- Services:  checkout-api, cart-store
- Repos:     org/checkout-api, org/cart-store
- Runtime:   checkout.prod (SLO 99.9%, p99 < 300ms)

## Consumes (X-as-a-Service)
- payments-api        (Payments team)
- db-as-a-service     (Platform team)

## How to work with us
- Requests:  #checkout-requests (Slack), async, reply < 4h
- Versioning: semver; breaking changes = 2-week notice
- On-call:   PagerDuty "checkout"

## Current interactions
- Collaboration w/ Payments — new refund flow — until Aug 15
```

That one file tells any other team *what we own, what we lean on, and how to reach us* — which is exactly the communication structure Conway said would become your architecture. Make it the structure you want.

**Try next:** List your current services in one column and the team that owns each in the next. Flag every service owned by more than one team, and every team that owns a *horizontal layer* rather than a business capability — those are your Conway hotspots. For the worst one, sketch the stream-aligned team that *should* own it end-to-end, and write its team API.
