---
title: "Zero trust for microservices: never trust the network"
date: 2026-07-26
track: microservices
summary: "Perimeter security assumes the network is safe once you're inside it; zero trust assumes it never is. This is the model — mTLS with short-lived SPIFFE SVIDs for service identity, JWTs validated at every hop (or exchanged) for user identity — with the mesh as one possible automation layer, not the point."
reading_time: 6
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

Saltmere's Istio ambient mesh article covered *how* mTLS gets automated at the infrastructure layer. This one is about the model underneath it — the thing you'd still need even if you ripped the mesh out. Sam Newman puts the perimeter assumption bluntly in *Building Microservices*: once an attacker is inside the network boundary, east-west traffic between services is often unauthenticated and unencrypted, because "we trusted the network." Zero trust is the rejection of that trust, formalized.

## Perimeter vs. zero trust

Perimeter security draws a hard shell around the network — firewall, VPN, DMZ — and treats everything inside as trusted. It fails for microservices specifically: internal traffic volume dwarfs external traffic, so the attack surface *inside* the perimeter is now enormous; a single compromised pod or leaked credential gives lateral access to everything on the same subnet; and cloud-native deployments don't have a stable perimeter to draw in the first place — services move across nodes, clusters, and clouds constantly.

NIST SP 800-207 defines zero trust as a set of concepts for making per-request access decisions under the assumption the network is already compromised. Its tenets are specific, not a slogan:

| Tenet (NIST SP 800-207) | What it means for a service mesh |
|---|---|
| All communication is secured regardless of network location | mTLS on every hop, not just at the edge |
| Access is granted per-session, least privilege | Short-lived certs and tokens, not standing trust |
| Access decided by dynamic policy (identity, posture) | Authorization checks identity claims, not source IP |
| Resource authentication/authorization is dynamic and strictly enforced | Every hop re-validates, none inherits trust from the previous hop |

The load-bearing phrase is "network location alone does not imply trust" — a request from inside the cluster gets exactly the same scrutiny as one from the public internet.

## Two identities, two mechanisms

Zero trust for microservices needs to answer two questions on every call: *which service is calling me* (workload identity), and *on whose behalf* (user identity). Conflating them is the usual mistake — a valid service certificate says nothing about which end user's request it carries, and a valid user JWT says nothing about which service is presenting it.

**Workload identity — mTLS + SPIFFE.** SPIFFE (Secure Production Identity Framework for Everyone) defines a URI-formatted identity, the SPIFFE ID (`spiffe://trust-domain/path`), and a document that proves it: the SVID (SPIFFE Verifiable Identity Document). The common form is the X.509-SVID — a short-lived certificate whose Subject Alternative Name is set to the SPIFFE ID as a URI SAN, with exactly one URI SAN per leaf certificate. SPIRE, the reference implementation, attests a workload's identity (via its Kubernetes service account, node, or process attributes) and issues it a rotating SVID — no long-lived secret ever touches the workload's filesystem. Two services then perform ordinary mTLS, except each side validates the peer's SPIFFE ID against a trust bundle and an authorization policy, not just "the cert chains to a CA I trust."

**User identity — JWTs via OAuth2/OIDC.** The end user authenticates once against an identity provider (Okta, Keycloak, Auth0, whatever implements OpenID Connect) and gets back an ID token and/or access token — a JWT with `sub` (subject), `aud` (audience), `exp` (expiry), and `iss` (issuer) claims, among others. That token — or a derivative of it — has to travel with the request through however many services handle it.

## Validating at every hop

The naive approach — validate once at the edge, then trust internal calls implicitly — is perimeter thinking wearing a JWT costume. Zero trust means every service that receives a token validates it independently:

```python
import jwt  # PyJWK / PyJWT
from jwt import PyJWKClient

jwks_client = PyJWKClient("https://idp.example.com/.well-known/jwks.json")

def validate_token(token: str, expected_audience: str):
    signing_key = jwks_client.get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=expected_audience,   # reject tokens minted for a different service
        issuer="https://idp.example.com/",
        options={"require": ["exp", "iat", "aud", "iss", "sub"]},
    )
    return claims  # exp/nbf checked automatically by pyjwt when present
```

Three checks matter every time, at every hop, not just at the perimeter:

- **Signature** — verify against the issuer's current JWKS, not a cached or hardcoded key, so a compromised or rotated key doesn't silently keep validating.
- **`aud`** — the token must be scoped to *this* service. A token minted for `orders-service` should be rejected by `billing-service` even if the signature is otherwise valid; this is what stops a leaked or replayed token from being useful everywhere.
- **`exp`** (and `nbf`/`iat`) — short lifetimes bound the damage window of a stolen token. This is the JWT analogue of SVID rotation.

The catch with "just forward the same JWT everywhere" is exactly that `aud` check: a token scoped to the edge gateway shouldn't also be valid three hops deep at a database-adjacent service, and a service shouldn't silently widen its own privileges by forwarding the user's raw token somewhere the user never intended to reach. That's the case for **OAuth 2.0 Token Exchange (RFC 8693)**: instead of forwarding, a service presents the inbound token to its identity provider's token endpoint (acting as an STS) and gets back a new, narrower token — scoped to the downstream `aud`, possibly with reduced scopes — carrying an `act` claim recording who is acting on whose behalf. RFC 8693 draws the line explicitly: **impersonation** means the downstream service can't tell A from B at all; **delegation** keeps both identities visible via `act`, preserving an audit trail across the hop. For anything crossing a trust boundary, delegation via token exchange beats blind forwarding.

## Where the mesh fits

None of this requires a service mesh — SPIRE issues SVIDs and libraries validate JWTs whether or not Envoy is in the picture. What a mesh (Istio, Linkerd, or ambient mode, covered separately on Saltmere) adds is automation: it can run SPIRE (or its own CA) to issue and rotate workload certs transparently, terminate and originate mTLS without application code touching TLS at all, and enforce `AuthorizationPolicy` rules keyed on SPIFFE identity. It typically does *not* handle the user-identity leg for free — JWT validation, `aud` checks, and token exchange stay in application code or a gateway filter (e.g., Envoy's JWT authentication filter), because only the application knows a given endpoint's authorization semantics. Treat the mesh as plumbing that makes "never trust the network" affordable at scale, not a replacement for validating identity yourself.

## Putting it together

A single hop should, at minimum: establish mTLS using rotating SVIDs (workload identity), validate the propagated or exchanged JWT's signature/`aud`/`exp` (user identity), and authorize on both — a valid cert from an unauthorized service, or a valid token with the wrong audience, is still a rejection. Do that at every hop and the network stops being a trust boundary; each service becomes its own perimeter.

**Try next:** stand up a local SPIRE server, register two workloads via Kubernetes service-account attestation, and inspect the resulting X.509-SVID with `openssl x509 -noout -text` to see the SPIFFE ID land in the URI SAN.
