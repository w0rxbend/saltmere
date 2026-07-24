---
title: "Sagas over two-phase commit: consistency without a distributed lock"
date: 2026-07-24
track: microservices
summary: "When a business operation spans several services, a distributed transaction is the tempting answer and usually the wrong one. Sagas trade atomicity for availability — here's how to build one."
reading_time: 5
tags: [sagas, transactions, consistency, orchestration, newman]
sources:
  - title: "Sam Newman, Building Microservices (2nd ed.), ch. 6 — Workflow"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Hector Garcia-Molina & Kenneth Salem, Sagas (1987)"
    url: "https://www.cs.cornell.edu/andru/cs711/2002fa/reading/sagas.pdf"
---

An order needs to reserve stock, charge a card, and schedule shipping — three services, one business outcome. The instinct is a distributed transaction so all three commit or none do. Newman's chapter 6 spends its energy talking you out of that: a two-phase commit (2PC) holds locks across every participant for the duration of the slowest one, turns independent services into a single availability unit, and still has failure windows between "prepare" and "commit". At microservice scale it's a latency and coupling tax you don't want to pay.

A **saga** takes the opposite bet: model the operation as a sequence of local transactions, each in one service, and for every step define a **compensating** action that semantically undoes it. No global lock; if a later step fails, you run the compensations for the steps already done.

## Orchestrated saga in pseudocode

An orchestrator owns the flow and issues compensations on failure. This is the easiest version to reason about and to debug:

```python
def place_order(order):
    done = []
    try:
        reserve = inventory.reserve(order.items);      done.append(("inventory", reserve))
        charge  = payments.charge(order.customer, order.total); done.append(("payments", charge))
        ship    = shipping.schedule(order);            done.append(("shipping", ship))
        return Ok(order.id)
    except StepError as e:
        for name, ref in reversed(done):        # compensate in reverse order
            if name == "shipping":  shipping.cancel(ref)
            if name == "payments":  payments.refund(ref)      # refund, not "un-charge"
            if name == "inventory": inventory.release(ref)
        return Failed(reason=e)
```

The important word is *semantic*. You cannot "un-charge" a card — you issue a **refund**. Compensations are new business actions, not rollbacks, and that's the mental shift sagas demand.

## The two hard parts (plan for them up front)

- **Idempotency.** A step may be retried after a timeout when it actually succeeded. Every step and every compensation must be safe to apply twice — pass a business idempotency key (the order id) so `charge` and `refund` de-duplicate server-side.
- **No isolation.** Between step 1 and step 3 the system is in a visible, partially-applied state — stock reserved but not yet paid. Someone can observe it. You handle that with semantic locks (mark the order `PENDING`) or by designing reads to tolerate in-flight orders, not by pretending the window doesn't exist.

## Orchestration vs choreography

The example above is *orchestration* — one coordinator. The alternative is *choreography*: each service emits events and the next reacts, no central brain. Choreography couples services more loosely but the overall workflow exists only as an emergent property of who-listens-to-what, which is murder to debug. Newman's practical advice: start orchestrated for anything with more than a couple of steps, reach for choreography when you specifically want to decouple teams and can invest in tracing to see the flow.

**Try next:** take a two-step operation in your system, write the compensating action for step one, and force step two to fail. If you can't cleanly express the compensation, that's a signal the two steps might belong in the same service — which is a legitimate answer a saga just helped you find.
