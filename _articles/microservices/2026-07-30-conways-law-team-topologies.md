---
title: "Conway's Law as a design tool: the Inverse Conway Maneuver in practice"
date: 2026-07-30
track: microservices
summary: "Service boundaries mirror the org chart whether or not that is planned for. A worked example of how a functionally-siloed organisation produces layered, wrong-grained services, and how re-organising into stream-aligned teams (Team Topologies) moves the boundaries to the source."
reading_time: 7
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

**Gist.** A system's module boundaries tend to reproduce the communication structure of the organisation that built it, so a boundary drawn on a whiteboard against the grain of the organisation degrades back towards the org chart. The Inverse Conway Maneuver treats that tendency as a lever: change the communication structure first — teams aligned to business capabilities rather than technical layers — so the desired architecture becomes the path of least resistance. The cost is that re-organising is slower, more disruptive and less reversible than redrawing a diagram, and it relocates specialists (database, platform, security) whose expertise previously concentrated in one place.

## The constraint

Melvin Conway's 1968 *Datamation* paper states the thesis directly:

> "organizations which design systems (in the broad sense used here) are constrained to produce designs which are copies of the communication structures of these organizations."

The operative word is **constrained**. The claim is not that architecture and organisation happen to resemble each other; it is that the design activity itself is bounded by who can talk to whom at what cost. An interface between two components is negotiated by the people who own those components. Where negotiation is cheap — same team, same standup — interfaces are renegotiated freely and boundaries move to wherever the problem wants them. Where negotiation is expensive — a ticket queue between departments — the interface **freezes at whatever shape it had when the two groups last agreed**, and subsequent design work routes around it rather than through it. Newman's treatment of organisational structures in *Building Microservices* works from the same constraint, arguing for aligning service ownership with the teams that deliver a capability.

## A worked example: three siloed teams, three wrong services

Consider a monolithic e-commerce application being decomposed by an engineering organisation structured by *technical function*:

- a **database administration (DBA) / data team** owning all schemas and stored procedures,
- a **middleware / backend team** owning application servers and business logic,
- a **frontend team** owning web and mobile user interfaces.

Communication is cheap inside each team and mediated by tickets across team boundaries. The predicted architecture follows the cheap paths:

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

The result is not a set of microservices but a **layered monolith with network calls between the layers**. It carries the coupling of the monolith — every layer must agree on a change — plus the operational cost of distribution.

The failure mode is visible in the change path for a single feature. A modification to *checkout* requires the frontend team to change the checkout screen, the middleware team to change checkout logic, and the DBA team to add a column and a stored procedure. **One business capability crosses three teams, three backlogs and three deployments**, sequenced by a release train because the three changes are not independently deployable. In-process calls that were previously function invocations are now network hops, so request latency accumulates across the layers and each hop adds a failure mode the monolith did not have. No team owns "checkout"; each owns **a horizontal slice of every capability**. The boundaries are real and enforced — they are cut along the wrong axis, and they copy the org chart precisely as the constraint predicts.

## The Inverse Conway Maneuver

The Thoughtworks Technology Radar names the technique the **Inverse Conway Maneuver**, describing it as evolving team and organisational structure so as to promote the desired architecture. Rather than treating the organisation as fixed and the architecture as freely chosen, it fixes the architecture as the goal and treats the organisation as the variable. The target for the example above is a **checkout** service, a **catalog** service and a **payments** service — vertical slices, each a business capability owned end-to-end by one team — so the teams are reshaped to match.

## Team Topologies: the shapes to re-organise into

Skelton and Pais supply a concrete target for the maneuver: four fundamental team types wired together by three defined interaction modes.

| Team type | Owns | In this example |
|---|---|---|
| **Stream-aligned** | A single value stream / business capability, end-to-end | `checkout`, `catalog`, `payments` teams, each with front-to-back skills |
| **Platform** | Internal self-service products that reduce load on stream teams | Continuous integration and delivery, Kubernetes, observability, database-as-a-service |
| **Enabling** | Coaching stream teams over a capability gap, then withdrawing | Former DBAs teaching teams to own their own schemas |
| **Complicated-subsystem** | A part requiring deep specialist knowledge | A pricing or fraud engine no stream team can hold entirely |

The **stream-aligned team is the default and the majority type**; the other three exist to keep it viable. The DBA and middleware silos dissolve into the stream teams or convert into platform and enabling teams. The checkout team then contains user-interface, service and data-model skills, and ships checkout without a cross-team ticket.

The three **interaction modes** make the remaining communication paths explicit rather than ambient:

- **Collaboration** — two teams work closely for a bounded period on unfamiliar terrain. High bandwidth, high cost, and the coupling it creates is exactly what Conway's constraint will imprint on the architecture, so it is used sparingly and ended deliberately.
- **X-as-a-Service** — one team consumes another's product through a stable interface with minimal ongoing discussion; the checkout team uses the platform's database-as-a-service without meetings.
- **Facilitation** — an enabling team helps another remove an impediment, then leaves.

The organising constraint behind the model is **cognitive load**: a team responsible for more domain than it can hold makes poor decisions and moves slowly. A stream-aligned team owning one capability, sitting on a platform that absorbs infrastructure concerns, can hold its domain. The former middleware team, owning a horizontal slice of a dozen domains, could not.

## Making the boundary legible: a team API

A re-organisation produces the intended architecture only if the new boundaries are explicit. Team Topologies proposes that each team publish a **team API** — a written description of what the team owns and how other teams interact with it, kept where the team's work lives and revised as those interactions change:

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

The file states what the team owns, what it depends on, and the cost and channel of reaching it — a written description of the communication structure that, by Conway's constraint, becomes the architecture. Writing it down makes the structure auditable: a team whose "owns" section lists a horizontal layer rather than a capability, or a collaboration entry with no end date, is a boundary drifting back towards the org chart.

## Pitfalls

- **Renaming silos as stream-aligned teams without moving ownership.** The team is called `checkout` but still files a ticket for every schema change; the delivery path still crosses three backlogs because the data model did not move with the label.
- **Splitting teams before a platform exists.** Each new stream team rebuilds deployment pipelines, monitoring and database provisioning, so cognitive load rises rather than falls and delivery slows immediately after the re-organisation.
- **Leaving collaboration mode open-ended.** Two teams intended to collaborate for one quarter remain coupled indefinitely; their services acquire a shared release cadence and stop being independently deployable.
- **Retaining a shared database under separated services.** The teams are vertical but the schema is common, so a column change still requires cross-team coordination and the old horizontal boundary survives beneath the new service boundaries.
- **Enabling teams that never withdraw.** An enabling team that stays permanently becomes a dependency in the delivery path, which reproduces the specialist silo it was created to dissolve.
- **A stale team API.** The document lists an interaction that ended and omits one that started, so other teams route requests through a path nobody owns and requests are dropped.
