---
title: "SPIFFE/SPIRE: a cryptographic name for every service instead of an API key"
date: 2026-07-31
track: microservices
summary: "Service-to-service authentication built on shared secrets degrades: keys are copied into environment variables, leak into logs, and rarely rotate. SPIFFE defines a universal workload identity — a URI carried in a short-lived credential — and SPIRE issues it from attested properties of the workload rather than from a secret the workload holds. The model and the attestation mechanism are described here."
reading_time: 6
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

**Gist.** Inter-service authentication is commonly implemented with a bearer credential — an API key or static token — where possession of the bytes is the entire proof of identity, so a leak is indistinguishable from legitimate use and rotation requires a coordinated redeploy. SPIFFE (Secure Production Identity Framework For Everyone) replaces the bearer credential with an identity **derived from attested properties of the calling process**, delivered as a short-lived certificate over a local socket; SPIRE (the SPIFFE Runtime Environment) is the reference implementation. The cost is an additional certificate authority with a signing key to protect, a per-node agent, and a registration policy that must be maintained with the same care as an access-control policy.

## A naming standard and a runtime that issues the names

**SPIFFE is a set of specifications, not software.** Its central construct is the **SPIFFE ID**, a Uniform Resource Identifier (URI) naming a workload:

```
spiffe://acme.example/ns/payments/sa/checkout
```

The authority component is the **trust domain** — a security boundary, conventionally one per organisation or environment. The path names a workload and its structure is chosen by the operator; the specification does not assign meaning to path segments.

The ID is carried inside an **SVID** (SPIFFE Verifiable Identity Document), of which the specifications define two forms:

- an **X.509-SVID**: an X.509 certificate carrying the SPIFFE ID in the URI Subject Alternative Name (SAN) field;
- a **JWT-SVID**: a signed JSON Web Token carrying the ID in the `sub` claim.

The distinction is load-bearing. **X.509-SVIDs are consumed by mutual TLS (mTLS)**, where identity is bound to the TLS handshake and therefore to the connection. **JWT-SVIDs are for paths where TLS is terminated by an intermediary** — a proxy or gateway — and a portable token must cross that boundary instead. A JWT-SVID is a bearer token once minted, so it reintroduces the replay exposure that X.509-SVIDs avoid; short lifetimes bound the exposure rather than removing it.

**SPIRE** is the reference runtime that issues SVIDs. It has two components:

- a **SPIRE Server**, the certificate authority for a trust domain, holding the signing key and the registration entries;
- a **SPIRE Agent**, one per node, which attests local workloads and serves SVIDs over a Unix domain socket.

SPIFFE and SPIRE graduated from the Cloud Native Computing Foundation (CNCF) incubator in 2022, and the SPIFFE ID format is used to name workloads by service meshes such as Istio.

## Attestation: why the credential is not a secret

The property that separates SPIFFE from "API keys with extra steps" is that **a workload never presents a secret to obtain its identity**. SPIRE establishes identity in two layers.

**Node attestation.** When a SPIRE Agent starts, it proves which node it is running on to the Server, using evidence the node cannot fabricate: a cloud provider instance identity document (Amazon Web Services, Google Cloud Platform, Azure), a Kubernetes projected service-account token, a Trusted Platform Module (TPM), and so on. The Server pins the agent to that node identity, which becomes the parent under which workload entries are registered.

**Workload attestation.** When a local process opens the Workload API socket and requests an SVID, the Agent inspects properties of the caller that the kernel reports rather than properties the caller asserts: the process user and group identifiers, the path of the executing binary, or — under Kubernetes — the pod's service account and labels, resolved through the kubelet. These observed properties become **selectors**.

The invariant is that **every attribute in a selector is observed by the Agent about the caller, never supplied by the caller**. A process cannot request a stronger identity than its own kernel-visible attributes support, because nothing it sends over the socket contributes to the decision.

Registration binds selectors to an ID ahead of time:

```bash
# A workload on this agent's node, in this Kubernetes namespace and under
# this service account, receives this SPIFFE ID.
spire-server entry create \
  -spiffeID spiffe://acme.example/ns/payments/sa/checkout \
  -parentID spiffe://acme.example/spire/agent/k8s_psat/prod/NODE \
  -selector k8s:ns:payments \
  -selector k8s:sa:checkout
```

The `-parentID` is the attested agent, so the entry is scoped to workloads on that node; the selectors are conjunctive — **all listed selectors must match the caller** for the entry to apply. When the checkout pod calls the Workload API, the Agent verifies membership in namespace `payments` under service account `checkout`, then mints an X.509-SVID. No value was pre-shared. Certificates are short-lived and rotated by the Agent without workload involvement, which bounds the value of a stolen certificate to its remaining validity rather than to the life of the deployment.

## Consumption: mTLS without secret handling

A workload connects to a local endpoint whose address it reads from the `SPIFFE_ENDPOINT_SOCKET` environment variable, as the Workload API specification requires, and receives a rotating certificate, its private key and the trust bundle. Using the Go library:

```go
// Fetches and auto-rotates the X.509-SVID; validates peers by SPIFFE ID.
source, _ := workloadapi.NewX509Source(ctx)
defer source.Close()

// Accept connections only from this peer identity.
authz := tlsconfig.AuthorizeID(
    spiffeid.RequireFromString("spiffe://acme.example/ns/orders/sa/api"))

server := &http.Server{
    Addr:      ":8443",
    TLSConfig: tlsconfig.MTLSServerConfig(source, source, authz),
}
```

Three consequences follow. **No certificate or key is mounted into the container**, so no file needs rotating on a schedule and none can be committed to a repository. **The trust bundle arrives over the same socket**, so certificate authority rotation propagates without redeployment. **Authorization is expressed over identity** — "only `orders/api` may call this service" — rather than over possession of a string, and the check runs during the handshake, before request handling.

The `AuthorizeID` form above accepts exactly one peer. Coarser policies replace it with a predicate over the peer ID; every such predicate reduces to a decision about the URI SAN presented in the peer certificate, which is the only identity the handshake conveys.

## Operational cost

SPIRE is a certificate authority in production. Its signing key requires the protection any CA key requires, its Agent runs on every node, and its registration entries must track deployment reality — a workload with no matching entry receives no identity and fails to establish mTLS, which manifests as a handshake failure rather than an authentication error. The failure in the opposite direction is quieter: **an over-broad selector set grants an identity to workloads it was not intended for**, and the resulting connections are cryptographically valid, so nothing in the logs marks them as anomalous. Selector rules therefore warrant the review discipline applied to access-control policy.

The behaviour of the model is observable in one command. Registering an entry keyed on a Unix user identifier and running `spire-agent api fetch x509` returns an SVID; running the same binary as a different user returns no identity. Identity is granted from what the process demonstrably is, and withheld when the observed properties do not match.

## Pitfalls

- **A JWT-SVID is a bearer token.** Anything that captures one — a log line, a proxy access record, an error report — can replay it until it expires; the X.509 path has no equivalent exposure, because presenting an X.509-SVID requires proving possession of its private key during the handshake, so the certificate alone is not usable.
- **Selectors are conjunctive, so a shortened selector list widens the grant.** An entry reduced to `k8s:ns:payments` issues the checkout identity to every pod in the namespace, and each such pod passes mTLS normally.
- **A missing registration entry surfaces as a TLS handshake failure, not an authentication denial.** The workload never obtains a certificate, so the peer sees a connection error and the cause is visible only in the Agent's view of attestation.
- **The Agent socket is the trust boundary on the node.** Any process able to open it is attested on its own kernel-visible properties, so a container sharing a process namespace or socket mount with another workload changes what selectors distinguish.
- **Node attestation ties agents to nodes.** An agent whose node evidence is no longer valid — a recycled instance identity document, a rotated projected token — fails attestation against the Server, and every workload depending on that agent loses its source of identity at once rather than individually.
- **Short certificate lifetimes make clock skew an authentication fault.** A node whose clock drifts beyond a certificate's validity window rejects peers holding freshly issued, correct certificates.
