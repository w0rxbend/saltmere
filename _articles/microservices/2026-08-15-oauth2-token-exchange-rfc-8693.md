---
title: "OAuth 2.0 Token Exchange (RFC 8693): Passing a User's Identity Down the Call Chain"
date: 2026-08-15
track: microservices
summary: "When service A calls service B which calls service C on behalf of a logged-in user, what does B send to C? Forwarding the raw access token overshares scope and audience; dropping the user identity breaks authorization and audit. RFC 8693 defines a token-exchange grant that mints a fresh, correctly-scoped token while recording the delegation chain in an 'act' claim. Keycloak 26.2 and Spring Security 6.3 both ship it, with the flow shown here as a curl example."
reading_time: 6
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

**Gist.** In a chain of services acting for a logged-in user, the intermediate hops must decide what credential to present downstream: forwarding the raw access token propagates the front door's full scope and audience to every hop, while calling downstream as a machine client discards the user's identity and with it per-user authorization and audit. **RFC 8693** (OAuth 2.0 Token Exchange, an IETF Proposed Standard) defines a grant at the authorization server's token endpoint that trades a held token for a fresh one, narrowed to the next hop's audience and scope, that still names the end user as subject. The cost is a synchronous round trip to the authorization server (AS) on every hop that exchanges, plus an exchange policy at the AS that becomes security-critical surface.

## The problem stated precisely

Consider an **API gateway** that receives a user's access token, calls an **orders** service, which in turn calls an **inventory** service. Two approaches fail in opposite directions.

**Forwarding the raw token** preserves identity but not confinement. The token's `aud` and `scope` were issued for the front door, so any hop holding it can present it anywhere that audience is accepted. A compromise two hops deep is a compromise of the original credential for its full remaining lifetime.

**Calling downstream with a client-credentials token** confines the credential but erases the subject. Inventory then sees only "orders-service", so it cannot enforce per-user authorization rules, and its audit log records the middle tier rather than the party the request was for.

Token exchange separates the two properties: **identity is carried in the `sub` claim, authority is carried in `aud` and `scope`, and the exchange re-derives the second while preserving the first.**

## The token-exchange grant

The grant is requested at the ordinary token endpoint with:

```
grant_type = urn:ietf:params:oauth:grant-type:token-exchange
```

The caller presents the token it already holds as the **`subject_token`** — the party being represented — and states what the new token is for.

| Parameter | Meaning |
|---|---|
| `subject_token` / `subject_token_type` | The token representing the party being acted for (the user) |
| `actor_token` / `actor_token_type` | Optional token identifying the *acting* party (the calling service) |
| `requested_token_type` | Desired output type, e.g. `...:token-type:access_token` |
| `audience` / `resource` | Who the new token is *for* — the downstream service |
| `scope` | The (usually narrowed) scopes requested for the new token |

Token types are identified by URNs: `urn:ietf:params:oauth:token-type:access_token`, `:jwt`, `:id_token`, `:refresh_token`, `:saml2`. **RFC 8693 makes `subject_token` and `subject_token_type` the only required parameters beyond `grant_type`;** `requested_token_type`, `audience`, `resource` and `scope` are all optional, and `actor_token_type` is required whenever `actor_token` is present. The response echoes an **`issued_token_type`**, which need not equal `requested_token_type` — the request expresses a preference, and the AS reports which type it minted. `token_type` is `Bearer` for access tokens and `N_A` otherwise.

## A concrete exchange

The orders service holds the user's access token and requires one scoped for inventory. It authenticates as a client and exchanges:

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

The issued token retains `sub` = the user, but its `aud` is `inventory-service` and its scope is trimmed to `inventory:read`. **Two independent checks therefore reject a replay of this token against the gateway: audience validation at the resource server, and scope evaluation at the endpoint.** The `expires_in` of 300 seconds bounds the window in which even a correctly-addressed replay succeeds.

Note the second authentication in the request: the exchange carries **both** the client credentials of the caller (`-u orders-service:…`) and the user's token. The AS is thus in a position to decide the exchange on the pair, not on the subject token alone.

## Impersonation versus delegation

RFC 8693 distinguishes two shapes of issued token.

- **Impersonation:** the new token is indistinguishable from one issued directly to the user. Same `sub`, no record of the intermediate. A downstream service cannot determine, and therefore cannot log, that a middle tier was involved.
- **Delegation:** the token names both parties. The subject remains the user, and an **`act`** (actor) claim identifies the party acting on the user's behalf. Where the caller supplies an `actor_token` alongside the `subject_token`, the AS composes the delegation from the two.

The `act` claim is a JSON object identifying the current actor, and it **nests** to represent a multi-hop chain, each new actor wrapping the previous `act`:

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

The nesting is read outermost-first: the **current** actor is the outer `act`, and each inner `act` is the actor that preceded it. **The invariant is that `sub` never changes along the chain — only the actor stack grows** — which is what makes the structure a provenance record rather than an identity substitution.

A companion **`may_act`** claim may be embedded in a token to declare which party is permitted to become an actor for that subject. It moves the authorization decision for the exchange into the subject token itself, allowing the AS to refuse an exchange whose caller is not the named party.

### Implementation sketch (Scala)

The load-bearing logic on the resource-server side is validating the actor chain rather than trusting `sub`. The nesting is a linked list, so both the push performed by the AS and the flattening performed by the verifier are short:

```scala
final case class Actor(sub: String, act: Option[Actor])
final case class Claims(
    sub: String, aud: String, scope: Set[String],
    act: Option[Actor], mayAct: Option[String])

/** The AS side: the new actor becomes the outermost `act`, wrapping the prior chain. */
def push(claims: Claims, newActor: String, audience: String, scopes: Set[String]): Claims =
  claims.copy(aud = audience, scope = claims.scope & scopes,   // narrowing only: never widens
              act = Some(Actor(newActor, claims.act)))

/** Outermost first: head is the most recent actor, last is the original front door. */
def chain(claims: Claims): List[String] =
  List.unfold(claims.act)(a => a.map(x => (x.sub, x.act)))

def admit(claims: Claims, self: String, allowedActors: Set[String]): Either[String, String] =
  chain(claims) match
    case Nil                                  => Left("impersonation token: no act claim")
    case current :: _ if !allowedActors(current) =>
      Left(s"actor $current not permitted to act for ${claims.sub}")
    case _ if claims.mayAct.exists(_ != chain(claims).head) =>
      Left("subject token names a different permitted actor")
    case _ if claims.aud != self              => Left(s"audience ${claims.aud} is not $self")
    case _                                    => Right(claims.sub)   // authorize as the user
```

Signature verification, issuer and expiry checks are omitted; they are unchanged from ordinary JSON Web Token (JWT) validation and are prerequisites, not substitutes, for the checks above.

## Implementation status

The grant is implemented, not spec-only. **Keycloak 26.2** (May 2025) promoted **standard token exchange** — the RFC 8693 grant — to officially supported status, replacing an older non-standard mechanism. **Spring Security 6.3** provides the client side via `TokenExchangeOAuth2AuthorizedClientProvider`, and **Spring Authorization Server 1.3** provides the AS side, so a Spring resource server can exchange an incoming token before calling downstream. Authlete documents the grant among its supported advanced flows.

## Pitfalls

- **Forwarding the exchanged token to a second downstream service fails audience validation.** The token was minted with a single `aud`; the second service rejects it, and the symptom is a 401 that looks like a signature problem but is an addressing problem.
- **Exchanging on every request adds an AS round trip to every hop.** The issued token's lifetime (300 seconds in the example above) is the cache window; without caching keyed by subject, audience and scope, the AS becomes a synchronous dependency on the request path.
- **An impersonation exchange leaves no `act` claim, so downstream audit logs attribute the call to the user with no record of the intermediate.** The absence is silent — nothing errors — and is only detectable by decoding an issued token and observing the missing claim.
- **A client permitted to exchange arbitrary subject tokens for arbitrary audiences can reach every service in the mesh.** The symptom is lateral movement that appears in logs as legitimate per-user access; the cause is exchange policy that constrains neither the subject nor the target audience per client.
- **Omitting `scope` may yield a token with the subject token's original scopes rather than a narrowed set,** removing the confinement the exchange was performed to obtain. Decoding the response is the only way to confirm the narrowing occurred.
- **`may_act` is only enforced if the AS reads it.** Embedding the claim in a subject token does not by itself prevent an exchange; the restriction holds only where the authorization server implements the check.
