---
title: "Zero trust for microservices: never trust the network"
date: 2026-07-26
track: microservices
summary: "Perimeter security treats a request as trustworthy once it is inside the network; zero trust does not. The model: mutual TLS with short-lived SPIFFE identity documents for workload identity, JWTs validated or exchanged at every hop for user identity, with a service mesh as one possible automation layer rather than the model itself."
reading_time: 7
tags: [zero-trust, mtls, spiffe, jwt, oauth2, oidc, microservices]
sources:
  - title: "NIST SP 800-207 — Zero Trust Architecture"
    url: "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-207.pdf"
  - title: "SPIFFE — X.509-SVID specification"
    url: "https://spiffe.io/docs/latest/spiffe-specs/x509-svid/"
  - title: "SPIFFE — JWT-SVID specification"
    url: "https://spiffe.io/docs/latest/spiffe-specs/jwt-svid/"
  - title: "RFC 8693 — OAuth 2.0 Token Exchange"
    url: "https://www.rfc-editor.org/rfc/rfc8693.html"
  - title: "OpenID Connect Core 1.0"
    url: "https://openid.net/specs/openid-connect-core-1_0.html"
---

**Gist.** Perimeter security authenticates at the network boundary and leaves east-west traffic between services unauthenticated, so one compromised workload has lateral reach over every peer on the same network. Zero trust replaces network location with per-request identity: **mutual TLS (mTLS) carrying short-lived workload credentials for "which service is calling", and a signed token validated independently at each hop for "on whose behalf"**. The cost is that every hop now performs cryptographic verification, every workload needs continuous credential rotation, and token audience scoping must be maintained per service rather than once at the edge.

Saltmere's Istio ambient mesh article covers *how* mTLS is automated at the infrastructure layer. This article describes the model underneath it — the part that remains necessary if the mesh is removed.

## Perimeter versus zero trust

Perimeter security draws a shell around the network — firewall, virtual private network (VPN), demilitarized zone (DMZ) — and treats everything inside as trusted. Three properties of microservice deployments undermine that assumption. Internal traffic volume dominates external traffic, so most of the attack surface sits inside the shell. A single compromised pod or leaked credential yields lateral access to peers reachable on the same network. And cloud-native deployments have no stable boundary to draw, since workloads move across nodes, clusters and clouds.

NIST Special Publication 800-207 defines zero trust as a set of concepts for making per-request access decisions under the assumption that the network is already compromised. Its tenets are specific rather than rhetorical:

| Tenet (NIST SP 800-207) | Consequence for service-to-service traffic |
|---|---|
| All communication is secured regardless of network location | mTLS on every hop, not only at the edge |
| Access is granted per-session, least privilege | Short-lived certificates and tokens, not standing trust |
| Access decided by dynamic policy (identity, posture) | Authorization checks identity claims, not source IP address |
| Resource authentication and authorization are dynamic and strictly enforced | Every hop re-validates; no hop inherits trust from its predecessor |

The load-bearing consequence is that **network location alone does not imply trust**: a request originating inside the cluster receives the same scrutiny as one from the public internet.

## Two identities, two mechanisms

Every call must answer two independent questions: *which workload is calling* and *on whose behalf*. Conflating them is the common error. A valid service certificate says nothing about which end user's request the connection carries, and a valid user token says nothing about which workload presented it.

**Workload identity — mTLS with SPIFFE.** SPIFFE (Secure Production Identity Framework for Everyone) defines a URI-formatted identity, the SPIFFE ID, of the form `spiffe://trust-domain/path`, together with a document that proves possession of it: the SVID (SPIFFE Verifiable Identity Document). The common form is the X.509-SVID, a short-lived certificate whose Subject Alternative Name (SAN) is the SPIFFE ID expressed as a URI SAN, with **exactly one URI SAN per leaf certificate**. That cardinality is what makes the peer identity unambiguous: verification code reads one URI SAN and compares it, rather than searching a set. SPIRE, the reference implementation, attests a workload (through its Kubernetes service account, node, or process attributes) and issues a rotating SVID, so no long-lived secret is written to the workload's filesystem. Two services then perform ordinary mTLS with one addition: **each side matches the peer's SPIFFE ID against a trust bundle and an authorization policy, not merely against a trusted certificate authority chain**. Chaining to the cluster CA is necessary and insufficient, because every workload in the trust domain chains to the same CA.

A separate form, the JWT-SVID, carries the SPIFFE ID as a signed token for cases where the transport cannot supply a client certificate.

**User identity — JWT via OAuth 2.0 and OpenID Connect.** The end user authenticates against an identity provider implementing OpenID Connect and receives an ID token, which OpenID Connect Core 1.0 defines as a JSON Web Token (JWT) carrying, among others, the `sub` (subject), `aud` (audience), `exp` (expiry) and `iss` (issuer) claims. An access token issued alongside it may or may not be a JWT; where it is, the same claims are available. That token, or a derivative of it, travels with the request through however many services handle it.

## Validating at every hop

Validating once at the edge and trusting internal calls thereafter reproduces the perimeter model with a token in place of a subnet. Under zero trust every service that receives a token verifies it independently. Three checks apply at every hop:

- **Signature**, verified against the issuer's current JSON Web Key Set (JWKS) rather than a hardcoded or indefinitely cached key, so that a rotated or revoked key stops validating.
- **`aud`**, restricting the token to *this* service. A token minted for `orders-service` is rejected by `billing-service` even when the signature verifies. This is the check that stops a leaked or replayed token from being usable everywhere.
- **`exp`**, with `nbf` and `iat` where present, bounding the window in which a stolen token is usable. It is the token-side analogue of SVID rotation.

The `aud` check is also what makes blind forwarding of the inbound token unsound: a token scoped to the edge gateway is not valid three hops deeper, and a service that forwards a user's raw token widens the reach of that token beyond where the user's request was directed. **OAuth 2.0 Token Exchange (RFC 8693)** addresses this. Rather than forwarding, the service presents the inbound token to a security token service (STS) endpoint and receives a new token scoped to the downstream audience, potentially with reduced scopes, carrying an `act` (actor) claim recording who is acting on whose behalf. RFC 8693 distinguishes the two shapes explicitly: under **impersonation** the downstream service cannot distinguish the acting party from the original subject, while under **delegation** both identities remain visible via `act`, which preserves the audit trail across the hop.

### Implementation sketch (Scala)

The per-hop invariant — a request is authorized only if *both* identities check out, and neither substitutes for the other — expressed as a single decision function. Signature verification is left abstract because it belongs to a JWT library; what the sketch fixes is the conjunction and the ordering.

```scala
final case class SpiffeId(uri: String)
final case class Claims(sub: String, aud: Set[String], iss: String, exp: Long)

enum Denied:
  case UntrustedPeer(id: SpiffeId)
  case WrongAudience(saw: Set[String])
  case Expired(at: Long)
  case BadSignature

trait TokenVerifier:
  /** Verifies the signature against the issuer's current JWKS and decodes. */
  def verify(jwt: String): Either[Denied, Claims]

final class HopPolicy(
    verifier: TokenVerifier,
    selfAudience: String,
    callersAllowed: Set[SpiffeId]   // peers permitted to reach this endpoint
):
  def authorize(peer: SpiffeId, jwt: String, now: Long): Either[Denied, Claims] =
    for
      _ <- Either.cond(callersAllowed(peer), (), Denied.UntrustedPeer(peer))
      c <- verifier.verify(jwt)
      _ <- Either.cond(c.aud(selfAudience), (), Denied.WrongAudience(c.aud))
      _ <- Either.cond(now < c.exp, (), Denied.Expired(c.exp))
    yield c
```

The peer's `SpiffeId` originates from the single URI SAN of the verified client certificate, never from a request header: a header is attacker-controlled on any hop that fails to terminate mTLS itself.

## Where the mesh fits

None of this requires a service mesh. SPIRE issues SVIDs and libraries validate JWTs irrespective of whether Envoy is present. A mesh (Istio, Linkerd, or ambient mode, covered separately on Saltmere) contributes automation: issuing and rotating workload certificates transparently, terminating and originating mTLS without application code handling TLS, and enforcing authorization rules keyed on SPIFFE identity. It does not generally cover the user-identity leg: JWT validation, `aud` checks and token exchange remain in application code or in a gateway filter such as Envoy's JWT authentication filter, because the authorization semantics of a given endpoint are known only to the application.

## Putting it together

A single hop establishes mTLS using rotating SVIDs, validates the propagated or exchanged token's signature, audience and expiry, and authorizes on both results. A valid certificate from an unauthorized workload, and a valid token with the wrong audience, are each a rejection on their own. Applied at every hop, the network ceases to be a trust boundary and each service becomes its own.

**Try next:** run a local SPIRE server, register two workloads via Kubernetes service-account attestation, and inspect the resulting X.509-SVID with `openssl x509 -noout -text` to observe the SPIFFE ID in the URI SAN.

## Pitfalls

- **Certificate chain validated, SPIFFE ID ignored.** Every workload in the trust domain chains to the same CA, so accepting any peer with a valid chain grants every workload access to every endpoint — mTLS is established, authorization is absent.
- **Verification algorithm taken from the token.** A validator that accepts whatever the header's `alg` field names, rather than pinning the algorithms it expects, lets an attacker choose one — the classic cases being `none` and reinterpreting an asymmetric public key as a symmetric HMAC secret.
- **JWKS cached without expiry.** A key rotated or revoked at the identity provider continues to validate tokens locally until the process restarts.
- **Audience unchecked, or checked only at the gateway.** A token minted for the edge remains usable at internal services, so a token leaked from one service authorizes calls to every other.
- **Raw token forwarded across a trust boundary.** The downstream service receives the user's full-scope token rather than a narrowed one, and the `act` chain that would record which service made the onward call does not exist.
- **Peer identity read from a request header.** Any hop that does not terminate mTLS itself is trusting an attacker-controlled string, since headers are not covered by the TLS peer certificate.
- **SVID lifetime long enough to survive an incident.** Short-lived credentials bound the damage window only if the lifetime is shorter than the detection-and-eviction time; a long-lived certificate reintroduces the standing trust the model removes.
- **Expiry checked without clock discipline.** Skew between issuer and validator rejects freshly issued tokens or accepts expired ones, and the failure appears as intermittent unauthenticated errors under load rather than as a clock problem.
