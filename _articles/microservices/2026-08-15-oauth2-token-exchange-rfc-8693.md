---
title: "OAuth 2.0 Token Exchange (RFC 8693): Passing a User's Identity Down the Call Chain"
date: 2026-08-15
track: microservices
summary: "When service A calls service B calls service C on behalf of a logged-in user, what does B send to C? Forwarding the raw access token overshares scope and audience; dropping the user identity breaks authorization and audit. RFC 8693 defines a token-exchange grant that mints a fresh, correctly-scoped token while recording the delegation chain in an 'act' claim. Keycloak 26.2 and Spring Security 6.3 both ship it — here's the flow with a real curl example."
reading_time: 5
tags: [oauth2, rfc-8693, token-exchange, identity, delegation, keycloak]
sources:
  - title: "RFC 8693 — OAuth 2.0 Token Exchange (IETF)"
    url: "https://datatracker.ietf.org/doc/html/rfc8693"
  - title: "Standard Token Exchange is now officially supported in Keycloak 26.2"
    url: "https://www.keycloak.org/2025/05/standard-token-exchange-kc-26-2"
  - title: "Token Exchange support in Spring Security 6.3.0-M3"
    url: "https://spring.io/blog/2024/03/19/token-exchange-support-in-spring-security-6-3-0-m3/"
  - title: "OAuth 2.0 Token Exchange (RFC 8693) — Authlete developer docs"
    url: "https://developers.authlete.com/protocols-and-flows/advanced-flows/oauth-2-0-token-exchange-rfc-8693"
---

A user calls your **API gateway**, which calls the **orders** service, which calls the **inventory** service. The user's access token arrived at the gateway. What flows to inventory? Two common answers are both wrong. **Forward the raw token** and every downstream hop gets the full scope and audience the user granted the *front door* — inventory can now call anything the gateway could, and a leak two hops deep is a leak of the original credential. **Drop the user identity** and call inventory as a machine client, and you've lost who the request is for: no per-user authorization, no honest audit trail. **RFC 8693** (OAuth 2.0 Token Exchange, an IETF Proposed Standard) is the sanctioned middle path — each hop exchanges the token it holds for a new one, correctly scoped for the next hop, that still carries the end user's identity.

## The token-exchange grant

Token exchange is a new grant type at the authorization server's token endpoint:

```
grant_type = urn:ietf:params:oauth:grant-type:token-exchange
```

The caller presents the token it already has as the **`subject_token`** — the identity being represented, the end user — and asks for a token aimed at the next service. Key parameters:

| Parameter | Meaning |
|---|---|
| `subject_token` / `subject_token_type` | The token representing the party being acted for (the user) |
| `actor_token` / `actor_token_type` | Optional token identifying the *acting* party (the calling service) |
| `requested_token_type` | Desired output type, e.g. `...:token-type:access_token` |
| `audience` / `resource` | Who the new token is *for* — the downstream service |
| `scope` | The (usually narrowed) scopes requested for the new token |

Token types are URNs: `urn:ietf:params:oauth:token-type:access_token`, `:jwt`, `:id_token`, `:refresh_token`, `:saml2`. The response echoes an **`issued_token_type`** and returns the new `access_token`, with `token_type` set to `Bearer` for access tokens (or `N_A` otherwise).

## A concrete exchange

The orders service holds the user's access token and needs one scoped for inventory. It authenticates as a client and exchanges:

```bash
curl -X POST https://auth.saltmere.dev/realms/shop/protocol/openid-connect/token \
  -u orders-service:$CLIENT_SECRET \
  -d 'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
  -d 'subject_token=eyJhbGciOiJSUzI1NiJ9.<user-access-token>' \
  -d 'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \
  -d 'requested_token_type=urn:ietf:params:oauth:token-type:access_token' \
  -d 'audience=inventory-service' \
  -d 'scope=inventory:read'
```

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
  "token_type": "Bearer",
  "expires_in": 300,
  "scope": "inventory:read"
}
```

The new token still has `sub` = the user, but its `aud` is `inventory-service` and its scope is trimmed to `inventory:read`. If it leaks, it can't be replayed against the gateway or anything else — it's a narrow, short-lived credential minted for exactly one hop.

## Impersonation vs. delegation

RFC 8693 draws a sharp line between two shapes of exchanged token.

- **Impersonation:** the new token looks like it came straight from the user. Same `sub`, no trace of the calling service. Downstream can't tell — and can't audit — that a middle tier was involved.
- **Delegation:** the new token names *both* parties. The subject stays the user, and an **`act`** (actor) claim records who is acting on their behalf. You send both a `subject_token` and an `actor_token`, and the AS composes the delegation.

The `act` claim is a JSON object identifying the current actor, and it **nests** to represent a multi-hop chain — each new actor wraps the previous `act`:

```json
{
  "sub": "user@saltmere.dev",
  "aud": "inventory-service",
  "act": {
    "sub": "orders-service",
    "act": {
      "sub": "api-gateway"
    }
  }
}
```

Read outward-in: the gateway acted, then orders acted on top of it, all on behalf of the user — a verifiable provenance chain. A companion **`may_act`** claim can be embedded in a token to declare, up front, which party is *permitted* to become an actor for that subject, letting the AS refuse unauthorized exchanges.

## Real support, and the caveats

This isn't spec-only. **Keycloak 26.2** (May 2025) promoted **standard token exchange** — the RFC 8693 grant — to officially supported, replacing its older non-standard mechanism. **Spring Security 6.3** (the client side, via `TokenExchangeOAuth2AuthorizedClientProvider`) and **Spring Authorization Server 1.3** (the AS side) both implement the grant, so a Spring resource server can exchange the incoming token before calling downstream. Authlete and other commercial servers support it as well.

The caveats are operational. Every hop that exchanges adds a round trip to the authorization server, so cache the issued tokens for their short lifetime. Impersonation tokens erase the audit chain — prefer delegation with `act` when you need traceability. And the AS's exchange policy is now security-critical surface: gate *which* clients may exchange *which* subjects with `may_act` and per-client policy, or you've built a lateral-movement machine instead of a delegation one.

**Try next:** stand up Keycloak 26.2, enable standard token exchange on a client, and exchange a user access token for a downstream-audience one — then decode both JWTs and diff the `aud`, `scope`, and `act` claims.
