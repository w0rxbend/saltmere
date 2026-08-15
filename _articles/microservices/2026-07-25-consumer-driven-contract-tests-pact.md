---
title: "Consumer-driven contracts with Pact: catching breaking changes without an integration environment"
date: 2026-07-25
track: microservices
summary: "End-to-end integration tests across services are slow, flaky, and prove less than they appear to. A consumer-defined contract plus provider verification catches breaking API changes at unit-test speed, and a Pact Broker's can-i-deploy answers whether a given version combination is safe to release."
reading_time: 6
tags: [contract-testing, pact, microservices, ci-cd, newman, testing]
sources:
  - title: "Sam Newman, Building Microservices (2nd ed.) — Testing (Ch. 9)"
    url: "https://www.oreilly.com/library/view/building-microservices-2nd/9781492034018/"
  - title: "Pact — How Pact works"
    url: "https://docs.pact.io/getting_started/how_pact_works"
  - title: "Pact-JS releases"
    url: "https://github.com/pact-foundation/pact-js/releases"
  - title: "Pact Broker — can-i-deploy"
    url: "https://docs.pact.io/pact_broker/can_i_deploy"
---

**Gist.** Verifying that two services still agree normally requires deploying both into a shared environment and exercising them together, which is slow, needs an environment that is rarely healthy in full, and couples one team's build result to another team's deploy. **Consumer-driven contract testing** replaces the shared environment with a file: the consumer records the requests it issues and the responses it requires against a mock provider, and the provider replays those recorded interactions against its real implementation in its own pipeline. The cost is a second artefact to manage — the contract and its verification results must be published, versioned and consulted before deployment, which is what the Pact Broker and its `can-i-deploy` check exist to do.

## What an end-to-end run establishes

Newman's testing chapter (Ch. 9 of *Building Microservices*, 2nd ed.) argues against treating end-to-end tests across real services as the strongest form of evidence. Three properties limit them. **Cost**: exercising one interaction requires standing up every service on the call path. **Ownership**: the environment is shared, so a failure has no single owner and a broken deploy elsewhere turns a consumer's build red. **Scope of the conclusion**: a green run demonstrates that the specific composition of versions deployed at that moment satisfied the assertions. It does not establish that the provider will continue to honour the response shape the consumer depends on, because nothing in the run records what that shape was.

Contract testing narrows the claim until it can be checked cheaply. The unit of evidence becomes a single request/response pair with a named precondition, and each side checks it independently.

## The consumer test generates the contract

With Pact-JS the consumer side is an ordinary unit test. The test declares an interaction, runs the **real client** against Pact's mock server, and emits a pact file if the assertions hold.

```javascript
import { PactV3, MatchersV3 } from '@pact-foundation/pact';
const { like } = MatchersV3;

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

Two details carry the weight.

The client under test is the production client, not a stub of it. The contract therefore records the requests the deployed code will genuinely send, including path construction and headers, rather than requests a test author believed it would send.

The **matchers pin shape rather than value**. `like(1299)` records the constraint "an integer in this position", so the provider satisfies the contract with any integer amount. A contract that pinned the literal `1299` would fail whenever seed data changed, producing failures that carry no information about compatibility. The consequence is symmetric and worth stating plainly: **anything the contract does not describe is unconstrained**. A field the consumer never reads may be removed by the provider without the verification noticing, which is the intended behaviour — the contract encodes the consumer's requirements, not the provider's full surface.

Passing the test writes `pacts/checkout-web-pricing-api.json`.

## The provider verifies against real code

The provider retrieves the pact and replays each interaction against its running service. Every `given(...)` string in the contract is a **provider state**: a named precondition the provider must establish before the request is issued. The state handler is the seam where the contract meets the provider's data model, and it is the only place the provider is permitted to arrange fixtures.

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

The verification loop per interaction is: run the state handler for the declared `given`, issue the recorded request against the real service, then compare the actual response against the recorded response under the recorded matching rules. A rename of `amountPence` to `amount` makes the body comparison fail. The failure appears **in the provider's own pipeline, against the provider's own code, with no consumer deployed** — which is the property that makes the check cheap enough to run on every commit. With `publishVerificationResult: true`, the outcome is recorded against `providerVersion`, so the result is attributable to a specific provider build rather than to "the provider".

Java Virtual Machine (JVM) teams obtain the same model from [pact-jvm](https://github.com/pact-foundation/pact-jvm), where provider verification runs as a JUnit 5 `PactVerificationInvocationContextProvider` that emits one test per interaction.

## The Broker and can-i-deploy close the loop

Contracts exchanged as files on disk do not survive more than a handful of teams: nothing records which provider version verified which contract. A **Pact Broker** (or the hosted PactFlow) stores every pact and every verification result, keyed by application version and tagged by environment. That inventory is what makes a release decision computable.

```bash
pact-broker can-i-deploy \
  --pacticipant checkout-web --version "$GIT_SHA" \
  --to-environment production --retry-while-unknown 30 --retry-interval 10
```

The question answered is narrow and exact: **for this specific consumer version, has every contract it participates in been verified by the provider versions currently recorded in the target environment?** A non-zero exit stops the deploy. The `--retry-while-unknown` and `--retry-interval` options cover the case where the answer is not yet known because a provider verification is still running, rather than known to be negative.

The operational consequence is that **consumer and provider may be released in either order**; the Broker withholds approval only from the combinations for which no verification evidence exists. A shared integration environment offers no equivalent record, because it tests whatever happens to be deployed at that instant and retains nothing about it.

## Pitfalls

- **A provider state handler that seeds data the consumer test never described** makes verification pass on behaviour the contract does not cover; the interaction then breaks in production while both suites stay green.
- **Pinning literal values instead of matchers** turns every seed-data change into a red verification, and teams respond by regenerating contracts without reading them, which removes the check entirely.
- **Fields the consumer never reads are absent from the contract**, so removing one passes verification. Contract tests bound compatibility with known consumers only; a consumer with no published pact is invisible to `can-i-deploy`.
- **Verifying without `publishVerificationResult`** leaves the Broker with no evidence for that provider version, and `can-i-deploy` reports unknown rather than success — the deploy gate blocks on missing data, not on incompatibility.
- **Running the consumer test against a hand-written stub client** rather than the production client records requests the deployed code never sends, and the provider verifies a contract nothing depends on.
- **Treating a passing pact as proof of end-to-end correctness** overstates it: the contract covers message shape and status per interaction, not sequencing, authentication in the deployed topology, latency, or business outcome.
