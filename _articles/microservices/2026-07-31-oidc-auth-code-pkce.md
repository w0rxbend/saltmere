---
title: "Authorization Code Flow with PKCE for a Browser Client and a Microservice API"
date: 2026-07-31
track: microservices
summary: "How the OpenID Connect authorization code flow with PKCE authenticates a browser single-page application against a microservice API, why the implicit grant was removed, and what the gateway checks in the JWT."
reading_time: 6
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

**Gist.** A single-page application (SPA) running in a browser is a *public* client: it ships its source to every visitor and therefore cannot hold a client secret, so any credential it receives over a redirect is exposed to whatever else can observe that redirect. The OpenID Connect (OIDC) authorization code flow with Proof Key for Code Exchange (PKCE, RFC 7636) returns only a single-use **authorization code** over the redirect and binds that code to a secret the client generated locally and never transmitted in the front channel, so an intercepted code cannot be redeemed. The cost is a second, back-channel round trip to the token endpoint, per-flow state the authorization server must retain between `/authorize` and `/token`, and the requirement that the client keep the verifier alive across a full-page redirect.

## The three roles

- **Authorization server (AS)** — authenticates the user and issues tokens. It owns the login user interface, the `/authorize` and `/token` endpoints, and the signing keys published as a JSON Web Key Set (JWKS) at the URI its discovery document advertises as `jwks_uri`.
- **Client** — the SPA. Public: no secret, no confidentiality for anything it holds.
- **Resource server (RS)** — the API or gateway. It validates tokens and serves data, and never handles user credentials.

## Access token versus ID token

OAuth 2.0 and OIDC answer different questions and produce different tokens.

- The **access token** (OAuth 2.0) is addressed to the *resource server*. It asserts that its bearer may call the API with the granted scopes; the RS is its audience.
- The **ID token** (OIDC Core 1.0) is addressed to the *client*. It is a JSON Web Token (JWT) carrying `iss`, `sub`, `aud`, `exp`, `iat` — and `nonce` when the authorization request supplied one — and it attests **who** authenticated. It renders the user in the SPA. It **must not be sent to the API as an authorization credential**: its `aud` names the client, so an RS that accepts it is accepting a token minted for a different audience.

## Why the implicit grant was removed

The implicit grant returned the access token itself in the redirect URI fragment. A live credential therefore landed in browser history, in `Referer` headers, and in any log that recorded URLs, with no evidence that the recipient was the party that initiated the flow. **RFC 9700 (BCP 240, January 2025)** directs clients not to use it. **OAuth 2.1 (draft-ietf-oauth-v2-1-15 — an Internet-Draft, not yet an RFC)** removes both the implicit and the resource-owner-password-credentials grants and **requires PKCE for the authorization code grant**. The code flow substitutes an opaque code that is worthless without a second exchange.

## What PKCE defends against

The threat is authorization-code interception. Because a public client holds no secret, possession of the code was once sufficient to redeem it — so malware, a rogue handler registered for the client's custom URI scheme, or a proxy that logs redirect URLs could exchange the code first. PKCE binds the code to a per-flow secret that never appears in the front channel.

1. Before redirecting, the client generates a random **`code_verifier`**: 43 to 128 characters from the unreserved set.
2. It derives a **`code_challenge`** and sends the challenge — not the verifier — to `/authorize`, together with `code_challenge_method=S256`.
3. At `/token` it sends the raw `code_verifier`. The AS recomputes `BASE64URL(SHA256(ASCII(verifier)))` and **rejects the exchange unless the result equals the challenge stored against that code**.

The invariant: the only value that traverses the front channel is a one-way hash. An attacker holding the code and the challenge still cannot produce a preimage, and the AS binds each code to exactly one challenge at issuance.

## Computing the S256 challenge

```bash
# Generate a verifier (32 random bytes -> 43 base64url chars)
code_verifier=$(openssl rand -base64 32 | tr '/+' '_-' | tr -d '=')

# S256: code_challenge = BASE64URL( SHA256( ASCII(verifier) ) )
printf '%s' 'dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk' \
  | openssl dgst -binary -sha256 | openssl base64 | tr '/+' '_-' | tr -d '='
# -> E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM
```

That verifier/challenge pair is the RFC 7636 Appendix B example, and the output above matches it byte for byte. The `plain` method sends the verifier itself as the challenge, which restores the interception exposure; `S256` is the method to use.

### Implementation sketch (Scala)

Verifier generation and the AS-side comparison, using only the standard library. The comparison is the load-bearing step: it is what makes a stolen code unredeemable.

```scala
import java.security.{MessageDigest, SecureRandom}
import java.util.Base64

object Pkce:
  private val b64url = Base64.getUrlEncoder.withoutPadding
  private val rng    = SecureRandom()

  /** 32 random bytes encode to 43 base64url characters, the RFC 7636 minimum. */
  def newVerifier(): String =
    val bytes = new Array[Byte](32)
    rng.nextBytes(bytes)
    b64url.encodeToString(bytes)

  def challengeS256(verifier: String): String =
    val digest = MessageDigest.getInstance("SHA-256")
      .digest(verifier.getBytes("US-ASCII"))
    b64url.encodeToString(digest)

  /** `MessageDigest.isEqual` does not stop at the first differing byte, so the
    * time it takes reveals nothing about how many leading characters matched.
    * Equal-length inputs are indistinguishable; a length mismatch is not. */
  def verify(storedChallenge: String, method: String, verifier: String): Boolean =
    val recomputed: Option[String] = method match
      case "S256"  => Some(challengeS256(verifier))
      case "plain" => Some(verifier)
      case _       => None
    recomputed.exists: candidate =>
      MessageDigest.isEqual(
        candidate.getBytes("US-ASCII"),
        storedChallenge.getBytes("US-ASCII"))
```

## The token request

Once the AS redirects back with `?code=...`, the client redeems the code in the back channel:

```bash
curl -s https://as.example.com/oauth/token \
  -d grant_type=authorization_code \
  -d client_id=spa-web \
  -d code=SplxlOBeZQQYbYS6WxSbIA \
  -d redirect_uri=https://app.example.com/callback \
  -d code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk
```

No client secret appears; the `code_verifier` is the proof of possession. The response carries `access_token`, `id_token`, and a `refresh_token` if the AS was asked for one and is configured to issue it to a public client.

## What the gateway checks

The RS is stateless with respect to sessions. For each request it reads the `Authorization: Bearer <jwt>` header and, against the AS's published JWKS, verifies:

1. **Signature** over the JWT's header and payload; the `kid` in the JWT header selects the key from the JWKS.
2. **`iss`** equals the expected authorization server.
3. **`aud`** includes this API's identifier.
4. **`exp`/`nbf`** — the token is neither expired nor not-yet-valid.
5. **`scope` or roles** cover the requested operation.

All five checks are local: **no network round trip per call** once the JWKS is cached. That property is what makes JWT access tokens tractable across a mesh of services — the gateway validates once and forwards the verified claims to downstream services.

The corresponding cost is revocation latency. A signature-only check cannot observe that a session was terminated after issuance, so a token remains acceptable until `exp`.

## Pitfalls

- **Sending the ID token to the API.** The RS's audience check fails, or worse, passes because the check was omitted; the ID token's `aud` names the client, not the API.
- **Using `code_challenge_method=plain`.** The verifier travels in the front channel exactly as the implicit grant's token did, so interception again yields a redeemable code.
- **Losing the verifier across the redirect.** A verifier held only in an in-memory variable is destroyed by the full-page navigation to `/authorize`; the callback then has a code it cannot redeem, and the flow fails at `/token` rather than at login.
- **Omitting or ignoring `nonce`.** Without binding the authorization request's `nonce` to the value in the returned ID token, the client cannot detect an ID token replayed from a different authentication.
- **Caching the JWKS without honouring `kid`.** After a key rotation the gateway holds only the retired key and rejects every freshly issued token with a signature error, which presents as a total outage rather than a partial one.
- **Treating a validated token as a live session.** Signature validation says the token was issued and has not expired; it says nothing about logout or revocation, so a terminated session keeps working until `exp`.
- **Redirect URI matching by prefix.** An AS that accepts any URI beginning with the registered value lets an attacker-controlled path receive the code; RFC 9700 requires exact string matching of redirect URIs.
