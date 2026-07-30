---
title: "Micro frontends: extending service independence past the browser boundary"
date: 2026-07-30
track: microservices
summary: "You split the backend into services owned by independent teams, then funnelled all of them through one monolithic single-page app. Micro frontends push the seam into the UI — here are the four composition approaches Newman lays out, and how Module Federation 2.0 wires them at runtime without shipping React three times."
reading_time: 5
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

You did the hard part. The backend is a set of independently deployable services, each owned by a stream-aligned team. Then all of that independence terminates at one place: a single monolithic single-page app that every team commits into, releases in lockstep, and coordinates around. Newman's point in the User Interfaces chapter of *Building Microservices* (2nd ed.) is blunt — a monolithic frontend re-centralizes exactly what you decentralized. **Micro frontends** are the pattern for pushing the ownership seam through the browser.

## Four ways to compose a UI from many teams

Newman and Cam Jackson's [martinfowler.com article](https://martinfowler.com/articles/micro-frontends.html) converge on the same menu of composition techniques.

- **Page-based decomposition.** Each page (or route) is a self-contained app owned by one team. Navigation between pages is a real browser navigation. This is the cheapest approach and the least coupled: teams share almost nothing at runtime. The cost is a full reload on cross-team navigation and no in-page composition of multiple teams.
- **Widget / component composition.** A single page hosts widgets owned by different teams — a search widget, a recommendations rail, a buy button. This is what most people mean by "micro frontends," and it's where the real integration problems live (see below).
- **iframes.** The oldest run-time isolation trick. iframes give you hard style and variable encapsulation almost for free. Jackson's warning stands: they make routing, deep-linking, history, and responsive sizing genuinely painful, and cross-frame communication is awkward. Fine for embedding a third-party or legacy island; poor as a primary architecture.
- **Server-side composition.** Fragments are stitched into the HTML on the server before it reaches the browser — Server Side Includes (SSI), Edge Side Includes, or a fragment-assembling gateway. [micro-frontends.org](https://micro-frontends.org/) leans on this for good first-paint performance, with one caveat: "the slowest fragment determines the response time of the whole page," so caching and selective async loading are mandatory.

These aren't mutually exclusive. A common shape is server-side composition for the shell and first paint, with run-time JavaScript composition for the interactive widgets inside it.

## Run-time composition: Module Federation

The dominant run-time mechanism today is **Module Federation**, originally shipped in Webpack 5 by Zack Jackson. As of mid-2026 the active project is **Module Federation 2.0** ([module-federation.io](https://module-federation.io/blog/announcement.html)), maintained by ByteDance's Web Infra team with the original authors. 2.0 extracted the runtime into a standalone SDK (decoupled from the bundler), added automatic TypeScript type generation across remotes, a `mf-manifest.json` manifest for version management, and Chrome DevTools. Crucially it runs on **Rspack** — the Rust-based bundler — as well as Webpack, via the shared `@module-federation/enhanced` plugin, with dramatically faster builds ([Rspack docs](https://rspack.rs/guide/features/module-federation)).

A *remote* exposes a component; a *host* consumes it at runtime by fetching the remote's `remoteEntry.js`. Here is a matched pair using `@module-federation/enhanced` (identical API on Webpack 5 and Rspack).

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

// shell/rspack.config.js — the HOST, the container app
new ModuleFederationPlugin({
  name: "shell",
  remotes: {
    // resolved at runtime, not build time — deploy checkout independently
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

Because `remotes` resolves the URL at *runtime*, the checkout team ships `BuyButton` without recompiling the shell. That is the whole point, and it's precisely what **build-time integration** (publishing each micro frontend as an npm dependency of the container) cannot do — Jackson warns that approach forces you to "re-compile and release every single micro frontend in order to release a change to any individual part of the product," which is the lockstep coupling you were escaping.

## The shared-singleton gotcha

Notice `shared` appears in both configs with `singleton: true`. This is the single most important — and most misconfigured — line. Without it, the host loads its React and the remote loads *its own* React. Two React instances on one page means hooks throw `Invalid hook call`, context providers don't cross the boundary, and your bundle carries React twice. `singleton: true` forces one shared copy; `requiredVersion` decides *which*. If the versions are incompatible (host on React 18, remote pins 17), Module Federation logs a version-mismatch warning and falls back — sometimes silently loading two copies anyway.

This is Newman's headline widget-composition tradeoff made concrete: **payload duplication and dependency versioning**. Five widgets from five teams, each dragging its own copy of a UI framework, and your page downloads megabytes of redundant runtime. `shared` singletons fix duplication but reintroduce coupling — now teams must agree on a React major version and coordinate upgrades, which is exactly the independence you were trying to buy back. There is no free lunch; there is only choosing where the coupling lives.

Two other escape hatches worth knowing. **Native Federation** (`@softarc/native-federation`, from Manfred Steyer / Angular Architects) reimplements the idea on browser-native **import maps** and ESM, so it isn't tied to Webpack and runs on esbuild or Vite. And `@module-federation/vite` brings the same API to Vite/Rollup, though as of 2026 its cross-remote dev HMR still trails the Rspack/Webpack path.

## When *not* to use micro frontends

They are an organizational solution to an organizational problem — multiple teams needing to ship a shared UI independently. If you have one team, or a handful of developers, you are buying distributed-systems overhead (versioning contracts, runtime loading failures, consistency drift, duplicated payloads) to solve a coordination problem you don't have. Newman's consistency warning is real: independently built widgets drift in look, feel, and behavior unless a shared design system and CSS-namespacing discipline is enforced across teams. And keep cross-widget communication minimal — custom events, URL state, or callbacks, never a shared in-memory store — or you've rebuilt the tangled monolith inside the browser.

**Try next:** Stand up the two configs above as separate Rspack apps, then deliberately break the singleton — set the remote's React to `^17.0.0` while the host stays on `^18.3.0`, load the page, and watch the version-mismatch warning and the `Invalid hook call`. Then add `strictVersion: false` versus `true` and compare the fallback behavior, so you know exactly what your users hit when two teams' upgrade cadences diverge.
