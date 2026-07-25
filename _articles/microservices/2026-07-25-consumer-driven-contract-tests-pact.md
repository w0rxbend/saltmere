---
title: "Consumer-driven contracts with Pact: catch breaking changes without an integration environment"
date: 2026-07-25
track: microservices
summary: "End-to-end integration tests across services are slow, flaky, and prove less than you think. A consumer-defined contract plus provider verification catches breaking API changes at unit-test speed — and a Pact Broker's can-i-deploy tells you whether it's safe to ship before you do."
reading_time: 5
tags: [contract-testing, pact, microservices, ci-cd, newman, testing]
sources:
  - title: "Sam Newman, Building Microservices (2nd ed.) — Testing (Ch. 9)"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Pact — How Pact works"
    url: "https://docs.pact.io/getting_started/how_pact_works"
  - title: "Pact-JS releases (latest v17.0.1, Jul 2026)"
    url: "https://github.com/pact-foundation/pact-js/releases"
  - title: "Pact Broker — can-i-deploy"
    url: "https://docs.pact.io/pact_broker/can_i_deploy"
---

Newman's testing chapter puts a fork in a common belief: that end-to-end tests across real services are the gold standard. They're the opposite. Spinning up every service to test one interaction is slow, needs a shared environment that's always half-broken, and turns *someone else's* flaky deploy into *your* red build. Worse, a green run tells you the fan-out worked *this time* — not that the provider will honour the shape your consumer depends on tomorrow.

**Consumer-driven contract testing** replaces that whole apparatus. The consumer writes a test against a *mock* of the provider, describing exactly the requests it makes and the responses it needs. That test generates a **pact file** — a JSON contract. The provider then replays those interactions against its real implementation. No shared environment, no orchestration, just two independent test suites that meet at a file.

## The consumer test generates the contract

With Pact-JS (v17.0.1, July 2026) the consumer side is an ordinary unit test. You declare an interaction, run your real client against Pact's mock server, and a pact file drops out.

```javascript
import { PactV3, MatchersV3 } from '@pact-foundation/pact';
const { like, eachLike } = MatchersV3;

const provider = new PactV3({
  consumer: 'checkout-web',
  provider: 'pricing-api',
  dir: './pacts',
});

describe('pricing client', () => {
  it('fetches a quote for a SKU', () => {
    provider
      .given('SKU 42 is priced')                     // provider state
      .uponReceiving('a quote request for SKU 42')
      .withRequest({ method: 'GET', path: '/quote/42' })
      .willRespondWith({
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        body: like({ sku: 42, currency: 'GBP', amountPence: like(1299) }),
      });

    return provider.executeTest(async (mockServer) => {
      const quote = await new PricingClient(mockServer.url).quote(42);
      expect(quote.amountPence).toBe(1299);           // real client, mock server
    });
  });
});
```

Notice the matchers. `like(1299)` says "any integer here", not "exactly 1299" — the contract pins the *shape*, not the sample values, so cosmetic data changes don't cause false failures. Passing this test writes `pacts/checkout-web-pricing-api.json`.

## The provider verifies against real code

The provider pulls that pact and replays each interaction against its running service. Each `given(...)` maps to a state-setup hook that seeds the data the interaction assumes. In Pact-JS:

```javascript
new Verifier({
  provider: 'pricing-api',
  providerBaseUrl: 'http://localhost:8080',
  pactBrokerUrl: process.env.PACT_BROKER_URL,
  publishVerificationResult: true,
  providerVersion: process.env.GIT_SHA,
  stateHandlers: {
    'SKU 42 is priced': async () => { await seedPrice(42, 1299); },
  },
}).verifyProvider();
```

If the provider renames `amountPence` to `amount`, its verification goes red — in the *provider's own* pipeline, against *its own* code, with no consumer deployed. That's the breaking change caught, days before it could reach production. JVM teams get the same model from [pact-jvm](https://github.com/pact-foundation/pact-jvm) (4.7.x) with a JUnit 5 `PactVerificationInvocationContextProvider`.

## The Broker and can-i-deploy close the loop

Files on disk don't scale across teams. A **Pact Broker** (or hosted PactFlow) stores every pact and every verification result, tagged by application version and environment. That inventory powers the one command that makes this safe in CI:

```bash
pact-broker can-i-deploy \
  --pacticipant checkout-web --version "$GIT_SHA" \
  --to-environment production --retry-while-unknown 30 --retry-interval 10
```

It answers a precise question: *for this exact version, has every contract it depends on been verified by the provider version currently in production?* If pricing-api hasn't yet verified the new quote interaction, `can-i-deploy` exits non-zero and the deploy stops — the consumer and provider can ship in any order, and the Broker refuses the combination that would break. That's the guarantee an integration environment only *pretends* to give you.

**Try next:** wire the consumer test above into CI so it publishes the pact to a Broker (`docker run pactfoundation/pact-broker` locally), add `can-i-deploy` as a deploy gate, then rename a field in the provider and watch its verification job — not your consumer's — go red.
