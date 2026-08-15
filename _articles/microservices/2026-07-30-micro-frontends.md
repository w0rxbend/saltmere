---
title: "Micro frontends: extending service independence past the browser boundary"
date: 2026-07-30
track: microservices
summary: "A backend split into independently deployable services loses that independence when every team commits into one monolithic single-page app. Micro frontends push the ownership seam into the UI — the four composition approaches Newman lays out, and how Module Federation 2.0 wires them at runtime without shipping React three times."
reading_time: 6
tags: [micro-frontends, module-federation, rspack, newman, ui-composition, web-components]
sources:
  - title: "Micro Frontends — extending the microservice idea to frontend development (micro-frontends.org)"
    url: "https://micro-frontends.org/"
  - title: "Cam Jackson — Micro Frontends (martinfowler.com)"
    url: "https://martinfowler.com/articles/micro-frontends.html"
  - title: "Module Federation 2.0 announcement (module-federation.io)"
    url: "https://module-federation.io/blog/announcement.html"
  - title: "Module Federation — Rspack docs"
    url: "https://rspack.rs/guide/features/module-federation"
  - title: "Sam Newman — Building Microservices, 2nd Edition (User Interfaces chapter)"
    url: "https://samnewman.io/books/building_microservices_2nd_edition/"
---

**Gist.** A backend decomposed into independently deployable services still releases in lockstep if every stream-aligned team commits into a single monolithic single-page application (SPA); the monolithic frontend re-centralizes what the backend decentralized. **Micro frontends** move the ownership seam through the browser, composing one page from fragments that separate teams build and deploy on separate schedules. The cost is paid in the browser: duplicated framework payloads, a runtime version-negotiation step that can fail after deployment rather than at build time, and visual drift between fragments that no compiler detects.

This is the argument Newman makes in the User Interfaces chapter of *Building Microservices* (2nd ed.), and the one Cam Jackson develops in the [martinfowler.com article](https://martinfowler.com/articles/micro-frontends.html).

## Four ways to compose a UI from many teams

Newman and Jackson converge on the same menu of composition techniques. They differ chiefly in **where composition happens** — at navigation time, in the browser at runtime, in a nested browsing context, or on the server before bytes reach the client — and each location determines which failures are possible.

- **Page-based decomposition.** Each page or route is a self-contained application owned by one team. Moving between pages is a real browser navigation, so the previous application's JavaScript heap, styles and globals are discarded by the browser itself. This is the cheapest approach and the least coupled: teams share close to nothing at runtime. The cost is a full document load on every cross-team navigation, and no way to compose two teams' output within one page.
- **Widget or component composition.** One page hosts widgets owned by different teams — a search box, a recommendations rail, a buy button. This is the shape most often meant by "micro frontends", and the one where the integration problems described below arise, because all fragments share a single JavaScript realm, a single global CSS cascade and a single history stack.
- **iframes.** A nested browsing context isolates styles and global variables by construction, since each frame is a separate document with its own realm. Jackson's warning stands: routing, deep linking, history and responsive sizing become difficult, and cross-frame communication is awkward. Suited to embedding a third-party or legacy island; poor as a primary architecture.
- **Server-side composition.** Fragments are stitched into the HTML before it reaches the browser — Server Side Includes (SSI), Edge Side Includes (ESI), or a fragment-assembling gateway. [micro-frontends.org](https://micro-frontends.org/) favours this for first-paint performance and records the accompanying failure mode plainly: **"the slowest fragment determines the response time of the whole page"**, so caching and selective asynchronous loading are treated as mandatory rather than optional.

These techniques are not mutually exclusive. A common arrangement uses server-side composition for the shell and first paint, with runtime JavaScript composition for the interactive widgets inside it.

## Runtime composition: Module Federation

The runtime mechanism in common use is **Module Federation**, originally shipped in Webpack 5 by Zack Jackson. The current line is **Module Federation 2.0** ([module-federation.io](https://module-federation.io/blog/announcement.html)), built by ByteDance's Web Infra team in collaboration with the original author. Version 2.0 extracted the runtime into a standalone software development kit (SDK) decoupled from the bundler, added automatic TypeScript type generation across remotes, introduced an `mf-manifest.json` manifest for version management, and shipped Chrome DevTools support. It runs on **Rspack** — a Rust-based bundler — as well as Webpack, through the shared `@module-federation/enhanced` plugin ([Rspack docs](https://rspack.rs/guide/features/module-federation)).

The vocabulary is two roles. A **remote** exposes one or more modules and publishes an entry file, conventionally `remoteEntry.js`. A **host** declares which remotes it consumes and by what URL; at runtime it fetches that entry file, which registers the remote's exposed modules and its shared-dependency declarations with the federation runtime. The load-bearing property is that **the remote's URL is resolved when the host executes, not when the host is compiled** — that is what allows the remote's team to deploy without rebuilding the host.

```js
// checkout/rspack.config.js — the REMOTE, owned by the checkout team
const { ModuleFederationPlugin } = require("@module-federation/enhanced/rspack");

module.exports = {
  plugins: [
    new ModuleFederationPlugin({
      name: "checkout",
      filename: "remoteEntry.js",
      exposes: {
        "./BuyButton": "./src/BuyButton.tsx",
      },
      shared: {
        react:       { singleton: true, requiredVersion: "^18.3.0" },
        "react-dom": { singleton: true, requiredVersion: "^18.3.0" },
      },
    }),
  ],
};

// shell/rspack.config.js — the HOST, the container application
new ModuleFederationPlugin({
  name: "shell",
  remotes: {
    // resolved at runtime, not build time — checkout deploys independently
    checkout: "checkout@https://checkout.example.com/remoteEntry.js",
  },
  shared: {
    react:       { singleton: true, requiredVersion: "^18.3.0" },
    "react-dom": { singleton: true, requiredVersion: "^18.3.0" },
  },
});
```

```jsx
// shell/src/ProductPage.tsx — consume the remote lazily
import { lazy, Suspense } from "react";
const BuyButton = lazy(() => import("checkout/BuyButton"));

export default () => (
  <Suspense fallback={<button disabled>Loading…</button>}>
    <BuyButton sku="t_porsche" />
  </Suspense>
);
```

The `lazy` wrapper matters for a reason beyond bundle size: `import("checkout/BuyButton")` performs a network fetch of a document the host does not control. A remote that is unreachable, or whose entry file has been replaced with an incompatible build, produces a rejected promise at render time. `Suspense` supplies the pending state; **an error boundary, not shown here, is what separates a degraded widget from a blank page.**

The alternative is **build-time integration** — publishing each micro frontend as an npm package that the container depends on. Jackson states the consequence: that approach forces a team to "re-compile and release every single micro frontend in order to release a change to any individual part of the product", reinstating the lockstep coupling the decomposition was meant to remove.

## The shared-singleton mechanism

`shared` appears in both configurations, with `singleton: true`. The declaration governs what the federation runtime does when host and remote both require the same package.

Without `singleton`, each side is free to use its own copy. Two React instances in one realm break the framework's invariants: hooks resolve against a module-level dispatcher that the rendering React instance sets, so a component rendered by one instance calling a hook imported from the other finds no dispatcher and throws `Invalid hook call`; context providers created by one instance are not readable by consumers of the other; and the page downloads the framework twice. `singleton: true` instructs the runtime to resolve the package to **one shared copy per page**, and `requiredVersion` states the range each side accepts. When the negotiated version satisfies neither range — a host on React 18 against a remote pinned to 17 — Module Federation reports a version mismatch and falls back. **`strictVersion` determines whether an unsatisfied range is an error or a warning**, and the warning path can end with two copies loaded after all.

This is the widget-composition trade-off made concrete: **payload duplication versus dependency versioning**. Five widgets from five teams, each carrying its own framework copy, make the page ship five copies of that framework instead of one. Shared singletons remove the duplication and reintroduce coupling in its place: the teams must now agree on a major version and coordinate upgrades. The coupling is not eliminated, only relocated — from the build to the release calendar.

Two adjacent implementations are worth naming. **Native Federation** (`@softarc/native-federation`, from Manfred Steyer and Angular Architects) rebuilds the idea on browser-native **import maps** and ECMAScript modules, so it is not tied to Webpack. `@module-federation/vite` brings the Module Federation API to Vite.

## When the pattern does not apply

Micro frontends address an organizational constraint: multiple teams needing to ship into one user interface on independent schedules. Applied to a single team, the pattern adds versioning contracts, runtime loading failures, consistency drift and duplicated payloads without a coordination problem to spend them on. Newman's consistency warning describes a failure with no automatic detector: independently built widgets diverge in appearance and behaviour unless a shared design system and a CSS-namespacing discipline are enforced across teams, and nothing in the build catches the divergence.

Cross-widget communication deserves the same restraint. Custom events, URL state and callbacks keep the fragments' contracts narrow and inspectable; a shared in-memory store recreates the monolith's coupling inside the browser while retaining the deployment complexity of the distributed version.

**Try next:** stand up the two configurations above as separate Rspack applications, then break the singleton deliberately — set the remote's React range to `^17.0.0` while the host stays on `^18.3.0`, load the page, and observe the version-mismatch report and the `Invalid hook call`. Compare `strictVersion: false` against `true` to see which fallback users encounter when two teams' upgrade cadences diverge.

## Pitfalls

- **Omitting `singleton: true` on the UI framework.** Symptom: `Invalid hook call` from remote components, and context providers in the host invisible to remote consumers. Cause: two framework instances in one JavaScript realm, each with its own module-level state.
- **Relying on `requiredVersion` alone without `strictVersion`.** Symptom: the page renders but the framework is downloaded twice. Cause: an unsatisfiable version range degrades to a warning and a fallback load rather than a hard failure.
- **No error boundary around a lazily imported remote.** Symptom: the whole page blanks when one team's CDN is unreachable. Cause: `import("remote/Thing")` rejects at render time and the rejection propagates past `Suspense`, which handles pending states and not failures.
- **Server-side composition without per-fragment caching or asynchronous loading.** Symptom: total page latency tracks the worst fragment. Cause: as micro-frontends.org states, the slowest fragment determines the response time of the whole page.
- **iframes chosen as the primary composition strategy.** Symptom: deep links do not restore state, the back button skips or repeats steps, and frames size incorrectly on narrow viewports. Cause: each frame is a separate browsing context with its own history and layout, not a participant in the parent document's.
- **Build-time integration presented as micro frontends.** Symptom: shipping a one-line change to one widget requires a release of the container and every other widget. Cause: npm-package integration binds fragment versions at compile time, so the container's build is the coordination point.
- **Unnamespaced global CSS in a widget.** Symptom: one team's deployment changes another team's rendering with no code change on the affected side. Cause: widget composition shares one global cascade; only iframes and shadow DOM isolate it.
