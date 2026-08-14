---
title: "WASI Preview 2 and the Component Model: Portable Services Without a Container"
date: 2026-08-14
track: microservices
summary: "WASI 0.2 gave WebAssembly a real host interface and the Component Model gave it polyglot composition. With WASI 0.3 shipping native async in June 2026 and Wasmtime at v47, you can run capability-secured services with millisecond cold starts — no container image required. Here's the current state and how to run one."
reading_time: 6
tags: [webassembly, wasi, component-model, wasmtime, spin, wit]
sources:
  - title: "WASI.dev — Introduction & roadmap"
    url: "https://wasi.dev/"
  - title: "WASI 0.3 Launched — Bytecode Alliance"
    url: "https://bytecodealliance.org/articles/WASI-0.3"
  - title: "The Component Model — WIT (interface definition)"
    url: "https://component-model.bytecodealliance.org/design/wit.html"
  - title: "Running components in Wasmtime — Component Model book"
    url: "https://component-model.bytecodealliance.org/running-components/wasmtime.html"
  - title: "WASI 0.2.0 and Why It Matters — wasmCloud"
    url: "https://wasmcloud.com/blog/wasi-preview-2-officially-launches/"
---

A container ships an entire userland to run one process. A WebAssembly component ships a sandboxed module that imports exactly the host capabilities it declares and nothing else. For a microservice that does one job, the second model is smaller, starts in milliseconds, and is deny-by-default secure. WASI 0.2 (Preview 2), launched in January 2024, is what made that practical for real services; the Component Model is what makes those services composable across languages.

## What WASI Preview 2 actually changed

WASI 0.1 was a flat list of POSIX-ish syscalls baked into a module's imports. WASI 0.2 rebuilt the interface on top of the **Component Model**: capabilities are now typed interfaces — `wasi:cli`, `wasi:http`, `wasi:filesystem`, `wasi:sockets` — that a component *imports* by name. Nothing is ambient. If a component never imports `wasi:sockets`, it physically cannot open a socket. As the Wasmtime docs put it, "by default, Wasmtime denies the component access to all system resources" — you grant filesystem or env access explicitly at launch. That is capability-based security enforced by the ABI, not by a policy layer bolted on top.

## WIT: the interface, not the implementation

Components describe their boundary in **WIT** (Wasm Interface Type). A `world` is the full contract — what a component imports and exports; an `interface` groups typed functions and records. This is the polyglot glue: a Rust component and a Go component that agree on a WIT world can be linked without either knowing the other's language.

```wit
package saltmere:pricing@1.0.0;

interface pricing {
    record price { sku: string, cents: u32 }
    lookup: func(sku: string) -> option<price>;
}

world service {
    import wasi:http/incoming-handler@0.2.0;
    export pricing;
}
```

Compile any language that has a component toolchain (Rust, Go via TinyGo, Python, JS via `jco`, C) against that world and you get a `.wasm` component with a machine-checkable interface — no shared SDK, no gRPC stubs to regenerate by hand.

## Running one, and composing many

Wasmtime (v47 as of August 2026) runs a component directly. There is no image to build or registry to pull — the artifact is the `.wasm` file:

```sh
# deny-by-default: this component gets no FS, no env, no network
wasmtime run ./pricing.wasm

# grant only what it needs
wasmtime run --dir ./data --env TIER=prod ./pricing.wasm

# serve an HTTP component (wasi:http)
wasmtime serve ./pricing.wasm
```

Composition happens *before* runtime with `wac`: link a component that exports `pricing` into one that imports it, producing a single fused component with no network hop between them.

```sh
wac plug ./checkout.wasm --plug ./pricing.wasm -o ./app.wasm
```

Higher-level platforms build on the same primitives. **Fermyon Spin 3.0** wraps this into a serverless developer flow (`spin new`, `spin up`), and **wasmCloud** distributes components across a mesh with capabilities supplied by pluggable host "providers." Fermyon's Spin runtime was acquired by Akamai and now runs Wasm Functions at edge scale — the pitch throughout is the same: cold starts in single-digit milliseconds because there's no OS to boot, just a module to instantiate.

## WASI 0.3 and native async

The big 2026 milestone: **WASI 0.3 launched June 11, 2026**, adding native async to the Component Model. Preview 2's async story was a manual `start`/`finish`/`subscribe` poll dance. WASI 0.3 puts `stream<T>`, `future<T>`, and `async func` directly in the canonical ABI, so — per the Bytecode Alliance — "the runtime, not each component, drives the scheduling," letting components share one host event loop. Wasmtime 46+ ships WASI 0.3 with async enabled by default; you opt in at the CLI:

```sh
wasmtime run -Sp3 -W component-model-async=y ./pricing.wasm
```

WASI 0.2 remains the stable, widely-supported target — most guest toolchains and platforms speak 0.2 today, with 0.3 guest support still rolling out language by language. For production services in 2026, target 0.2 and watch 0.3 land.

The trade-offs are real: the guest-toolchain ecosystem is younger than containers', debugging tools are thinner, and long-running stateful workloads still favor a full OS. But for stateless, security-sensitive, fast-scaling services — request handlers, plugins, edge functions — a WASI component is a smaller and tighter unit of deployment than an image.

**Try next:** install `wasmtime` and `cargo component`, write the `world` above, and run the resulting `.wasm` twice — once plain and once with `--dir` — to watch capability-based security deny and then grant filesystem access.
