---
title: "SPIFFE/SPIRE: giving every service a cryptographic name instead of an API key"
date: 2026-07-31
track: microservices
summary: "Service-to-service auth built on shared secrets rots: keys get copied into env vars, leak into logs, and never rotate. SPIFFE defines a universal workload identity — a URI in a short-lived certificate — and SPIRE issues those identities based on what a workload provably is, not a secret it holds. Here's the model and how attestation actually works."
reading_time: 5
tags: [spiffe, spire, mtls, zero-trust, workload-identity, svid]
sources:
  - title: "SPIFFE Concepts — official docs (spiffe.io)"
    url: "https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/"
  - title: "SPIRE Concepts — server, agent, workload attestation"
    url: "https://spiffe.io/docs/latest/spire-about/spire-concepts/"
  - title: "The SPIFFE ID and SVID specifications (GitHub)"
    url: "https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE-ID.md"
  - title: "SPIFFE Workload API (Endpoint) specification"
    url: "https://github.com/spiffe/spiffe/blob/main/standards/SPIFFE_Workload_API.md"
  - title: "CNCF — SPIFFE and SPIRE graduation announcement"
    url: "https://www.cncf.io/announcements/2022/09/20/spiffe-and-spire-projects-graduate-from-cloud-native-computing-foundation-incubator/"
---

Look at how one of your services authenticates to another and you will usually find a shared secret: an API key or a bearer token, injected as an environment variable, baked into a Helm value, copied into a `.env` that someone once pasted into Slack. It rarely rotates, because rotating it means coordinating a redeploy across every consumer. It is, in the zero-trust sense, a bearer credential — whoever holds the bytes *is* the caller. SPIFFE exists to delete that entire category of secret.

## SPIFFE is a naming standard, SPIRE is the thing that hands out names

**SPIFFE** (Secure Production Identity Framework For Everyone) is a set of specs, not software. Its core idea is a universal identifier for a workload, the **SPIFFE ID**, shaped like a URI:

```
spiffe://acme.example/ns/payments/sa/checkout
```

The host is the **trust domain** (a security boundary — one per org or environment); the path names a workload however you like. That ID is carried inside an **SVID** (SPIFFE Verifiable Identity Document), which comes in two forms: an **X.509-SVID** (an X.509 certificate with the SPIFFE ID in the URI SAN) or a **JWT-SVID** (a signed JWT with the ID in `sub`). X.509-SVIDs are what you use for mutual TLS between services; JWT-SVIDs are for cases where a proxy or gateway terminates TLS and you need a portable token.

**SPIRE** is the reference runtime that produces these. It has two pieces:

- a **SPIRE Server** — the certificate authority for a trust domain, holding the signing key and the registration policy;
- a **SPIRE Agent** — one per node, which attests workloads locally and hands them SVIDs over a Unix domain socket.

SPIFFE graduated in the CNCF alongside SPIRE, and the model now underpins the identity layer in meshes like Istio.

## The part that makes it not-a-secret: attestation

The reason SPIFFE isn't "API keys with extra steps" is *how a workload proves it deserves an identity*. It never presents a secret. Instead SPIRE performs **attestation** in two layers:

- **Node attestation:** when a SPIRE Agent starts, it proves *which node* it runs on to the Server — via a cloud instance-identity document (AWS/GCP/Azure), a Kubernetes projected service-account token, a TPM, etc. The Server pins the agent to that node identity.
- **Workload attestation:** when a local process asks the Agent for an SVID over the socket, the Agent inspects the *caller's own kernel-visible properties* — its UID/GID, its Linux binary path, or, in Kubernetes, its pod's service account and labels via the kubelet. These become **selectors**.

You register, ahead of time, which selectors map to which SPIFFE ID:

```bash
# "A workload in this k8s namespace + service account gets this identity"
spire-server entry create \
  -spiffeID spiffe://acme.example/ns/payments/sa/checkout \
  -parentID spiffe://acme.example/spire/agent/k8s_psat/prod/NODE \
  -selector k8s:ns:payments \
  -selector k8s:sa:checkout
```

Now the checkout pod calls the Workload API socket, the Agent verifies it really is in `payments` running as `checkout`, and only then mints a short-lived X.509-SVID for it. Nothing was pre-shared. The identity is derived from *what the workload verifiably is*, and the certificate typically lives on the order of an hour, rotated automatically — so a leaked cert is worthless almost immediately, and there is no long-lived key to steal.

## Consuming it: mTLS with zero secret handling

Your service talks to a local socket (the default path is `/tmp/spire-agent/public/api.sock`) and gets a rotating cert bundle. With the Go library:

```go
// Fetches and auto-rotates the X.509-SVID; validates peers by SPIFFE ID.
source, _ := workloadapi.NewX509Source(ctx)
defer source.Close()

// Only accept connections from a specific peer identity.
authz := tlsconfig.AuthorizeID(
    spiffeid.RequireFromString("spiffe://acme.example/ns/orders/sa/api"))

server := &http.Server{
    Addr:      ":8443",
    TLSConfig: tlsconfig.MTLSServerConfig(source, source, authz),
}
```

There is no certificate file to mount, no key to rotate on a cron, and no CA bundle to distribute — the source keeps them fresh in memory. Authorization becomes a statement about *identity* ("only `orders/api` may call me"), not about possession of a secret.

## Where the effort really goes

SPIRE is not free to run. You are standing up a CA with a signing key that must itself be protected, plus an agent DaemonSet, plus a registration policy that someone has to keep in sync with reality. The failure mode is subtle too: if attestation is misconfigured you can hand identities to the wrong workloads, so the selector rules deserve the same review rigor as IAM policy. The payoff is that "which service is calling" stops being a guess based on a shared string and becomes a cryptographically attested fact.

**Try next:** run SPIRE locally with the quickstart, register a single entry keyed on your Unix `uid`, and fetch an SVID with `spire-agent api fetch x509`. Then run the same binary as a *different* user and watch the fetch return no identity. That refusal — identity granted from what the process provably is, denied when it isn't — is the whole model in one command.
