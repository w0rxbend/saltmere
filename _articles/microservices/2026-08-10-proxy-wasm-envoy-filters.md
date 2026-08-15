---
title: "Proxy-Wasm: WebAssembly Filters for Envoy and Istio"
date: 2026-08-10
track: microservices
summary: "How the Proxy-Wasm ABI extends Envoy and Istio with sandboxed WebAssembly filters shipped independently of the proxy build — with a Rust HttpContext example and the Envoy and Istio WasmPlugin deploy configs."
reading_time: 7
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

**Gist.** Envoy is the data plane under most service meshes, and custom behaviour — stamping a header, enforcing a token, emitting a metric, rewriting a body — historically required either a C++ filter welded to the proxy build or a small Lua snippet embedded in the request path. Proxy-Wasm defines a proxy-agnostic application binary interface (ABI) so a filter compiled to WebAssembly (Wasm) loads into an unmodified proxy at runtime, sandboxed in its own linear memory. The cost is the sandbox boundary itself: every header read and body chunk crosses it as a host call with copies, and the embedded Wasm runtime adds memory per virtual machine (VM).

## What the sandbox buys and what it charges

Three properties distinguish Wasm from the two older extension paths.

**Memory isolation.** A Wasm module executes over a linear memory region with **no ambient access to the host process**. Its only reach into the proxy is the explicit set of host-call functions the ABI declares. A panic, an out-of-bounds write, or a non-terminating loop is contained within the module rather than the proxy. A native C++ filter shares the proxy's address space, so the same defect is a segmentation fault in the serving process.

**Independent release cadence.** A native filter is compiled into the Envoy binary, so every Envoy version bump forces a recompile and revalidation of the fork. A Wasm module is a **separate artifact** — a `.wasm` file or an Open Container Initiative (OCI) image — versioned, signed and rolled out on its own schedule against a stock proxy.

**Language choice.** Because the contract is an ABI rather than a C++ API, any language with a Proxy-Wasm software development kit (SDK) can produce a filter: Rust (`proxy-wasm-rust-sdk`), Go/TinyGo (`proxy-wasm-go-sdk`), C++, and AssemblyScript.

The charge is equally concrete. Modules pay marshalling overhead on each crossing of the sandbox boundary, cold start and per-request copies are not free, and the runtime embedded in Envoy — V8 or Wasmtime — consumes memory. For latency-critical hot paths a native filter remains faster; Wasm is the correct choice where isolation and independent delivery outweigh the last microsecond.

## The ABI and the host↔module model

Proxy-Wasm is specified in [`proxy-wasm/spec`](https://github.com/proxy-wasm/spec) and defines calls in two directions:

- **Module → host**, the `proxy_*` imports: the module asks the proxy to read a header, send a response, set shared data, or dispatch an HTTP call.
- **Host → module**, the `proxy_on_*` exports: the proxy notifies the module of lifecycle and traffic events — VM start, plugin configuration, new stream, request headers, response body, log.

The specification publishes the ABI as versioned revisions — **0.1.0, 0.2.0 and 0.2.1** — with 0.2.1 the version mainstream hosts implement. Envoy is the reference host, but the same ABI is implemented by NGINX (Kong's `ngx_wasm_module`), Apache Traffic Server, and MOSN, so a module built to the specification is portable across them.

The execution model has **two context types**. A **RootContext** exists once per plugin per worker and owns plugin-level configuration and the plugin lifecycle. For each request — or each TCP stream — the proxy asks the root context to mint a **stream context**, an `HttpContext` for HTTP filters, which receives the per-request callbacks. Per-request state therefore lives in the stream context and is discarded with the stream; state that must outlive a request belongs on the root context or in the shared-data host calls. The SDKs map these two contexts directly onto traits.

## A minimal Rust filter

The following filter uses the [Rust SDK](https://github.com/proxy-wasm/proxy-wasm-rust-sdk) (crate `proxy-wasm`). It inspects the inbound `x-api-key` header, rejects the request with status `403` when the header is absent or empty, and otherwise stamps `x-wasm-filter` before the request proceeds upstream.

{% raw %}
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
                // Header added here is visible to the upstream service.
                self.add_http_request_header("x-wasm-filter", "proxy-wasm");
                Action::Continue
            }
            _ => {
                // Response is produced in the filter; upstream is never contacted.
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
{% endraw %}

Every identifier above is real SDK surface: `set_root_context`, the `Context` / `RootContext` / `HttpContext` traits, `get_type` returning `ContextType::HttpContext`, `create_http_context`, `on_http_request_headers`, `get_http_request_header`, `add_http_request_header`, `send_http_response`, and the `Action` variants. The return value drives the filter chain: **`Action::Continue` allows the chain to proceed, `Action::Pause` stops it** — here because the response has already been produced locally.

The module is built as a `cdylib` for the Wasm target. The crate's `Cargo.toml` sets `crate-type = ["cdylib"]`:

```bash
rustup target add wasm32-wasip1
cargo build --target wasm32-wasip1 --release
# -> target/wasm32-wasip1/release/apikey_filter.wasm
```

Older guides target `wasm32-unknown-unknown`; recent SDK releases build against `wasm32-wasip1`, the renamed `wasm32-wasi`.

## Deployment to a standalone Envoy

Outside a mesh the module is loaded as an HTTP filter in the connection manager, here on Envoy's embedded V8 runtime:

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

**Ordering is load-bearing: the Wasm filter must precede the `router` filter**, because the router terminates the chain by forwarding upstream. `code.local.filename` names the module on disk; `code.remote` fetches it from a URI and validates it against a declared SHA256 digest.

## Deployment through Istio's WasmPlugin

Inside a mesh the Envoy configuration is generated rather than hand-edited. The `WasmPlugin` custom resource ([API reference](https://istio.io/latest/docs/reference/config/proxy_extensions/wasm-plugin/)) is the supported path for Wasm extensions, in place of hand-written `EnvoyFilter` resources. The module is packaged as an OCI image, pushed to a registry, and referenced:

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

`phase` places the module within Istio's filter chain: **`AUTHN` runs before Istio's own authentication filters, `AUTHZ` after authentication, `STATS` near telemetry, and `UNSPECIFIED_PHASE` places it at the end, before the router**. `priority` orders multiple plugins sharing a phase, with higher values first. The `pluginConfig` structure is delivered to the root context's configuration callback, so one module serves several workloads with different settings. The Istio agent pulls the OCI artifact, caches it locally, and loads it into the sidecar without a proxy restart.

## Pitfalls

- **The Wasm filter placed after `envoy.filters.http.router`** never observes the request: the router forwards upstream and terminates the chain, so the filter's callbacks appear to be dead code.
- **Returning `Action::Continue` after `send_http_response`** lets the chain proceed even though a response was already emitted locally; the pause is what prevents the upstream call.
- **Storing per-request state on the RootContext** leaks across concurrent streams, because one root context serves every stream on that worker while stream contexts are created and destroyed per request.
- **Building for `wasm32-unknown-unknown` with a recent SDK release** produces a module that may fail to build or to instantiate, because the SDK's generated code expects the WASI imports the `wasm32-wasip1` target supplies.
- **Omitting `crate-type = ["cdylib"]`** yields an rlib rather than a loadable `.wasm` module, and the build succeeds without producing the artifact the proxy needs.
- **Assuming an ABI version the host does not implement** breaks the module at VM start rather than at request time; the host refuses to instantiate a VM whose declared ABI it does not recognise.
- **Treating a per-request body rewrite as free** ignores the copies made at each crossing of the sandbox boundary, which is where Wasm loses to a native filter on latency-critical paths.
