---
title: "Proxy-Wasm: Writing WebAssembly Filters for Envoy and Istio"
date: 2026-08-10
track: microservices
summary: "How the Proxy-Wasm ABI lets you extend Envoy and Istio with sandboxed WebAssembly filters shipped independently of the proxy build — with a working Rust HttpContext example and the Envoy and Istio WasmPlugin deploy configs."
reading_time: 6
tags:
  - envoy
  - istio
  - webassembly
  - service-mesh
  - rust
sources:
  - title: "proxy-wasm/spec — WebAssembly for Proxies ABI specification"
    url: "https://github.com/proxy-wasm/spec"
  - title: "proxy-wasm/proxy-wasm-rust-sdk"
    url: "https://github.com/proxy-wasm/proxy-wasm-rust-sdk"
  - title: "Istio — WasmPlugin API reference"
    url: "https://istio.io/latest/docs/reference/config/proxy_extensions/wasm-plugin/"
  - title: "Envoy — Wasm HTTP filter (wasm.proto, api-v3)"
    url: "https://www.envoyproxy.io/docs/envoy/latest/api-v3/extensions/wasm/v3/wasm.proto"
  - title: "Solo.io — The State of WebAssembly in Envoy Proxy"
    url: "https://www.solo.io/blog/the-state-of-webassembly-in-envoy-proxy-f2b3f"
---

Envoy is the data plane under most service meshes, and sooner or later you want it to do something it doesn't do out of the box: stamp a header, enforce a token, emit a custom metric, rewrite a body. Envoy has always been extensible — but historically your options were to fork it and write a C++ filter (rebuild the whole proxy, ship a custom binary, chase every upstream release) or to embed Lua for small in-request logic. Proxy-Wasm is the third door: compile your filter to a WebAssembly module and load it into an unmodified proxy at runtime.

## Why Wasm instead of native C++ or Lua

Three properties make Wasm compelling for proxy extension.

**A real sandbox.** A Wasm module runs in a linear-memory sandbox with no ambient access to the host. It can only touch the proxy through a narrow, explicit set of host-call functions defined by the ABI. A panic, an out-of-bounds write, or an infinite loop is contained to the module — it does not take down the proxy process. Native C++ filters share the proxy's address space, so a bug is a segfault in production.

**Independent shipping.** A native filter is welded to the Envoy build. Every Envoy bump means recompiling and revalidating your fork. A Wasm module is a separate artifact — a `.wasm` file, or an OCI image — that you version, sign, and roll out on its own cadence against a stock proxy. That decoupling is the whole point: your release train stops depending on Envoy's.

**Multiple languages.** Because the contract is an ABI rather than a C++ API, you write filters in whatever language has a Proxy-Wasm SDK: Rust (`proxy-wasm-rust-sdk`), Go/TinyGo (`proxy-wasm-go-sdk`), C++, and AssemblyScript. Lua is fine for a five-line tweak; it is not where you want to build and test a stateful authz filter.

The cost is real too: Wasm modules pay a marshalling overhead crossing the sandbox boundary, cold-start and per-request copies aren't free, and the runtime (V8 or Wasmtime, embedded in Envoy) adds memory. For hot-path, latency-critical work, native still wins. Wasm is the right tool when safety and independent delivery matter more than the last microsecond.

## The ABI and the host↔module model

Proxy-Wasm is standardized as a proxy-agnostic ABI in [`proxy-wasm/spec`](https://github.com/proxy-wasm/spec). It defines two directions of calls:

- **Module → host** (`proxy_*` imports): the module asks the proxy to do things — read a header, send a response, set shared data, dispatch an HTTP call.
- **Host → module** (`proxy_on_*` exports): the proxy notifies the module of lifecycle and traffic events — VM start, plugin config, new stream, request headers, response body, log.

The published ABI versions are **0.1.0, 0.2.0, and 0.2.1**; 0.2.1 is the current one that mainstream hosts implement. It is stable enough to build on but still evolving (an ABI 0.3 is discussed upstream). Envoy is the reference host, but the same ABI is implemented by NGINX (Kong's `ngx_wasm_module`), Apache Traffic Server, MOSN, and others — a module built to the spec is portable across them.

The execution model has two context types. A **RootContext** exists once per plugin per worker and owns plugin-level config and shared lifecycle. For every request (or TCP stream) the proxy asks the root to mint a **stream context** — an `HttpContext` for HTTP filters — which receives the per-request callbacks. The SDK maps these directly onto traits.

## A minimal Rust filter

Here is a complete HTTP filter using the [Rust SDK](https://github.com/proxy-wasm/proxy-wasm-rust-sdk) (crate `proxy-wasm`, current published `0.2.5`). It checks for an inbound `x-api-key` header, rejects the request with `403` if it is missing, and otherwise stamps an `x-wasm-filter` header before the request continues upstream.

```rust
use log::info;
use proxy_wasm::traits::*;
use proxy_wasm::types::*;

// Registers the module with the host and wires up the root context.
proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Info);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(ApiKeyRoot)
    });
}}

struct ApiKeyRoot;

impl Context for ApiKeyRoot {}

impl RootContext for ApiKeyRoot {
    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }

    // Called once per HTTP stream to create a fresh per-request context.
    fn create_http_context(&self, context_id: u32) -> Option<Box<dyn HttpContext>> {
        Some(Box::new(ApiKeyFilter { context_id }))
    }
}

struct ApiKeyFilter {
    context_id: u32,
}

impl Context for ApiKeyFilter {}

impl HttpContext for ApiKeyFilter {
    fn on_http_request_headers(&mut self, _num_headers: usize, _end_of_stream: bool) -> Action {
        match self.get_http_request_header("x-api-key") {
            Some(key) if !key.is_empty() => {
                info!("#{} authorized request", self.context_id);
                // Add a header the upstream can see.
                self.add_http_request_header("x-wasm-filter", "proxy-wasm");
                Action::Continue
            }
            _ => {
                // Short-circuit: reply directly from the filter, don't hit upstream.
                self.send_http_response(
                    403,
                    vec![("content-type", "text/plain")],
                    Some(b"missing x-api-key\n"),
                );
                Action::Pause
            }
        }
    }
}
```

Every name here is real SDK surface: `set_root_context`, the `Context` / `RootContext` / `HttpContext` traits, `get_type` returning `ContextType::HttpContext`, `create_http_context`, `on_http_request_headers`, `get_http_request_header`, `add_http_request_header`, `send_http_response`, and the `Action::Continue` / `Action::Pause` return values. `Continue` lets the filter chain proceed; `Pause` stops it — here because we've already produced the response.

Build it as a cdylib for the Wasm target. The crate's `Cargo.toml` sets `crate-type = ["cdylib"]`; compile with:

```bash
rustup target add wasm32-wasip1
cargo build --target wasm32-wasip1 --release
# -> target/wasm32-wasip1/release/apikey_filter.wasm
```

(Older guides target `wasm32-unknown-unknown`; recent SDK releases build against `wasm32-wasip1`, the renamed `wasm32-wasi`.)

## Deploying to raw Envoy

For a standalone Envoy, load the module as an HTTP filter in the connection manager. The runtime is Envoy's embedded V8:

```yaml
http_filters:
- name: envoy.filters.http.wasm
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.wasm.v3.Wasm
    config:
      name: apikey_filter
      root_id: ""
      vm_config:
        runtime: envoy.wasm.runtime.v8
        code:
          local:
            filename: /etc/envoy/apikey_filter.wasm
- name: envoy.filters.http.router
  typed_config:
    "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
```

The Wasm filter must sit before the `router` filter so it can act on the request. `code.local.filename` points at the module on disk; `code.remote` can fetch it from a URI with a SHA256 instead.

## Deploying through Istio's WasmPlugin

In a mesh you don't hand-edit Envoy config — Istio does it for you. The `WasmPlugin` custom resource ([API ref](https://istio.io/latest/docs/reference/config/proxy_extensions/wasm-plugin/)) is the supported path (it superseded raw `EnvoyFilter` for Wasm). Package the `.wasm` as an OCI image, push it to a registry, and reference it:

```yaml
apiVersion: extensions.istio.io/v1alpha1
kind: WasmPlugin
metadata:
  name: apikey-filter
  namespace: default
spec:
  selector:
    matchLabels:
      app: reviews          # workloads to inject into
  url: oci://ghcr.io/saltmere/apikey-filter:1.0.0
  imagePullPolicy: IfNotPresent
  phase: AUTHN              # AUTHN | AUTHZ | STATS | UNSPECIFIED_PHASE
  priority: 100             # higher runs first within a phase
  pluginConfig:
    header_name: x-api-key  # arbitrary config handed to the module
```

`phase` decides where in Istio's filter chain your module lands — `AUTHN` runs before Istio's own auth filters, `AUTHZ` after authentication, `STATS` near telemetry, and `UNSPECIFIED_PHASE` drops it at the end before the router. `priority` orders multiple plugins in the same phase. The `pluginConfig` struct is delivered to your RootContext's config callback, so the same module can be reused with different settings per workload. Istio's agent pulls the OCI artifact, caches it locally, and hot-loads it into the sidecar — no proxy restart, no mesh redeploy.

## Where this leaves you

Proxy-Wasm gives you a safe, language-flexible, independently-shipped extension point for the proxy that already fronts your services. The ABI is stable at 0.2.1, the Rust and Go SDKs are production-usable, and Istio's WasmPlugin makes rollout a `kubectl apply`. Start with something small — a header check like the one above — measure the latency cost on your own traffic, and grow from there.

**Try next:** rebuild the filter to read `header_name` from `pluginConfig` in `on_configure`, package it as an OCI image with `buildah`/`docker`, and roll it to a single namespace via a `WasmPlugin` selector before widening the rollout.
