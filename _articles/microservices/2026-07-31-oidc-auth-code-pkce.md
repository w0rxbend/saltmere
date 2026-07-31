---
title: "Authorization Code Flow with PKCE: The Only Way to Log Users Into an SPA + API"
date: 2026-07-31
track: microservices
summary: "How OIDC/OAuth authorization code flow with PKCE authenticates a browser SPA against a microservice API, why implicit flow is dead, and how the gateway validates the JWT."
reading_time: 5
tags: [oauth, oidc, pkce, jwt, microservices, security]
sources:
  - title: "RFC 7636 — Proof Key for Code Exchange by OAuth Public Clients"
    url: "https://www.rfc-editor.org/rfc/rfc7636"
  - title: "RFC 9700 (BCP 240) — Best Current Practice for OAuth 2.0 Security"
    url: "https://www.rfc-editor.org/info/rfc9700/"
  - title: "The OAuth 2.1 Authorization Framework (draft-ietf-oauth-v2-1-15)"
    url: "https://datatracker.ietf.org/doc/html/draft-ietf-oauth-v2-1-15"
  - title: "OpenID Connect Core 1.0"
    url: "https://openid.net/specs/openid-connect-core-1_0.html"
  - title: "Curity — The OAuth Code Flow"
    url: "https://curity.io/resources/learn/oauth-code-flow/"
---

You have a single-page app in the browser and a set of backend microservices behind a gateway. You need the user logged in and every API call authorized. The correct answer, in 2026, is the **OpenID Connect authorization code flow with PKCE**. Nothing else.

## The three roles

- **Authorization server (AS)** — issues tokens after authenticating the user. Owns the login UI, `/authorize` and `/token` endpoints, and the signing keys (`/.well-known/jwks.json`).
- **Client** — your SPA. It is a *public* client: it ships to the browser and holds no secret.
- **Resource server (RS)** — your API / gateway. It validates tokens and serves data. It never talks to the SPA about credentials.

## Access token vs ID token

OAuth 2.0 and OIDC solve different problems and hand you different tokens:

- The **access token** (OAuth) is for the *resource server*. It says "this bearer may call the API with these scopes." The RS is its audience.
- The **ID token** (OIDC) is for the *client*. It is a JWT with `iss`, `sub`, `aud`, `exp`, `iat`, and `nonce` that proves *who* logged in. The SPA reads it to render the user; it must **not** be sent to the API as an authorization credential.

## Why implicit flow is dead

The old implicit flow returned the access token directly in the redirect URL fragment. That put a live credential in browser history, `Referer` headers, and server logs, with no proof the receiver was the party that started the flow. **RFC 9700 (BCP 240, Jan 2025)** tells clients not to use it, and **OAuth 2.1 (draft-15, March 2026, still an Internet-Draft, not yet an RFC)** removes the implicit and resource-owner-password grants entirely and makes **PKCE mandatory for all clients**. Use code flow, get an opaque code, exchange it out-of-band.

## What PKCE actually defends against

A public client can't keep a secret, so the authorization code is the weak link: malware, a rogue custom-URI-scheme handler, or a logging proxy could intercept it and redeem it. PKCE (RFC 7636) binds the code to a one-time secret the attacker never sees.

1. Before redirecting, the SPA generates a random **`code_verifier`** (43–128 unreserved chars).
2. It derives a **`code_challenge`** and sends *that* on `/authorize`, along with `code_challenge_method=S256`.
3. On `/token`, it sends the raw `code_verifier`. The AS recomputes the challenge and rejects the exchange unless it matches.

An intercepted code is useless without the verifier, which never left the client.

## Computing the S256 challenge

```bash
# Generate a verifier (32 random bytes -> 43 base64url chars)
code_verifier=$(openssl rand -base64 32 | tr '/+' '_-' | tr -d '=')

# S256: code_challenge = BASE64URL( SHA256( ASCII(verifier) ) )
printf '%s' 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk' \
  | openssl dgst -binary -sha256 | openssl base64 | tr '/+' '_-' | tr -d '='
# -> E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM
```

That verifier/challenge pair is the canonical RFC 7636 Appendix B example — the output above matches it byte for byte. Always use `S256`, never `plain`.

## The token request

After the AS redirects back with `?code=...`, the SPA redeems it:

```bash
curl -s https://as.example.com/oauth/token \
  -d grant_type=authorization_code \
  -d client_id=spa-web \
  -d code=SplxlOBeZQQYbYS6WxSbIA \
  -d redirect_uri=https://app.example.com/callback \
  -d code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
```

No client secret — the `code_verifier` is the proof. The response returns `access_token`, `id_token`, and typically a `refresh_token`.

## How the gateway validates the JWT

The RS is stateless. On each request it takes the `Authorization: Bearer <jwt>` header and, using the AS's published JWKS, checks:

1. **Signature** against the AS public key (`kid` in the header picks the key).
2. **`iss`** equals the expected authorization server.
3. **`aud`** includes this API's identifier.
4. **`exp`/`nbf`** — not expired, not future-dated.
5. **`scope`/roles** cover the requested operation.

All local, no network round-trip per call — which is exactly why JWT access tokens fit a microservices mesh: the gateway validates once and forwards the verified claims downstream.

**Try next:** Stand up a local Keycloak, register a public client with PKCE required, and run the flow by hand — copy the `code` out of the redirect, redeem it with the curl above, then decode the returned access token at jwt.io and confirm `iss`, `aud`, and `exp` are exactly what your gateway would check.
