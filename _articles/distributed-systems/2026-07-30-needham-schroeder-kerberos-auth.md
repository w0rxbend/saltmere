---
title: "Needham–Schroeder, the replay flaw, and the clock Kerberos adds"
date: 2026-07-30
track: distributed-systems
summary: "The Needham–Schroeder symmetric-key protocol is the ancestor of Kerberos: a trusted server mints session keys so two parties who share no secret can authenticate. Denning and Sacco showed that one leg of the message flow carries no freshness guarantee. This article walks the five messages, the replay attack, and the timestamp fix Kerberos substitutes for the missing round trip."
reading_time: 7
tags: [security, authentication, needham-schroeder, kerberos, nonce, replay-attack]
sources:
  - title: "Using Encryption for Authentication in Large Networks of Computers — Needham & Schroeder (CACM 1978)"
    url: "https://dl.acm.org/doi/10.1145/359657.359659"
  - title: "A Logic of Authentication (BAN logic) — Burrows, Abadi, Needham (1990)"
    url: "https://www.cs.utexas.edu/~byoung/cs361/BAN.pdf"
  - title: "Distributed Systems (4th ed.), van Steen & Tanenbaum — §9 Security, authentication"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "The Kerberos Network Authentication Service (V5) — RFC 4120"
    url: "https://datatracker.ietf.org/doc/html/rfc4120"
  - title: "Authentication Protocols — Computer Networks: A Systems Approach (Peterson & Davie)"
    url: "https://book.systemsapproach.org/security/authentication.html"
---

**Gist.** Two hosts that share no secret and distrust the network between them cannot bootstrap a session key on their own, so Needham and Schroeder (1978) introduce a trusted third party that shares a long-term key with each host and mints a fresh session key for the pair. The mechanism proves freshness with nonces — values used once and echoed back inside ciphertext — but only one of the two parties receives that protection, and Denning and Sacco showed in 1981 that the unprotected leg admits replay. Kerberos closes the gap by moving freshness into the ticket itself as a **timestamp and lifetime**, which avoids the extra round trip a nonce-based repair needs at the cost of requiring every participant to keep a loosely synchronised clock.

## The setup

Alice (A) and Bob (B) each share a long-term symmetric key with a Key Distribution Center (KDC): `K_A` and `K_B` respectively. No other principal holds either key. The KDC mints a fresh **session key** `K_AB` and delivers it to both parties, each copy encrypted so only the intended holder can read it. Notation: `{X}K` denotes X encrypted under key K.

```
1. A -> KDC:  A, B, N_a
2. KDC -> A:  {N_a, B, K_AB, {K_AB, A}K_B}K_A
3. A -> B:    {K_AB, A}K_B
4. B -> A:    {N_b}K_AB
5. A -> B:    {N_b - 1}K_AB
```

Every field carries weight:

- **Message 1** names the intended peer and carries a fresh nonce `N_a`. It travels in the clear; it reveals who wishes to talk to whom but no key material.
- **Message 2** is encrypted under `K_A`, so only Alice can open it. It contains four fields. The echoed nonce `N_a` binds the reply to *this* request, so a recorded older reply is rejected. Bob's name binds the session key to the peer Alice asked for, so an attacker who rewrites message 1 cannot substitute a different party without Alice noticing. `K_AB` is the new session key. The final field is a sealed **ticket** `{K_AB, A}K_B`, opaque to Alice, which she can only forward.
- **Message 3** delivers the ticket. Bob decrypts it under `K_B`, obtains `K_AB`, and reads the claim that his peer is Alice.
- **Messages 4–5** form a challenge–response that convinces Bob his peer is live: Bob sends a nonce under the session key, and Alice returns a predictable transform of it, `N_b − 1`. Only a principal holding `K_AB` can compute the transform, and the decrement is what distinguishes the answer from a verbatim echo of Bob's own message.

The invariant the protocol aims for is that at the end of message 5, both principals hold `K_AB`, each believes the other holds it, and neither has accepted a value that predates its own contribution to the exchange.

## Where the invariant breaks

Denning and Sacco identified the gap in 1981: **message 3 carries no freshness guarantee for Bob.** The ticket `{K_AB, A}K_B` contains no nonce from Bob, no counter, and no expiry. Nothing in it distinguishes a ticket minted a second ago from one minted a year ago.

The consequence is an attack conditioned on one compromise. Suppose an adversary records a complete run and later recovers that run's session key `K_AB` — by offline analysis, by compromising an endpoint that retained it, or by any other means. The adversary replays the recorded message 3 to Bob. Bob decrypts the ticket, accepts `K_AB`, issues a fresh challenge `N_b`, and the adversary — holding `K_AB` — answers correctly. **Bob completes the protocol believing he is speaking to Alice, while Alice takes no part in the run at all.**

The asymmetry is the point. Alice's nonce in message 2 gives her a freshness guarantee that survives any later key compromise, because a stale message 2 fails the nonce check. Bob's challenge in message 4 proves the peer holds `K_AB` *now*, but says nothing about whether `K_AB` is current. **The compromise of a single expired session key grants indefinite impersonation of Alice to Bob.**

## Two repairs

**Needham and Schroeder's own revision (1987)** adds a round in which Bob contributes a nonce before the ticket is minted, so the ticket is bound to a value Bob knows to be fresh. This preserves the pure-nonce design and its independence from clocks, and pays for it with additional messages.

**Kerberos** places the freshness data inside the ticket instead: `{K_AB, A, timestamp, lifetime}K_B`. Bob accepts the ticket only if the timestamp falls within an allowed clock-skew window — **commonly configured at five minutes** — and the lifetime has not elapsed. A replayed ticket presents an old timestamp and is rejected as expired without any interaction with the KDC or with Alice. Separately the client sends an **authenticator**, a timestamp encrypted under the session key, which demonstrates that the sender holds `K_AB` at the present moment rather than at some point in the past.

The trade is a round trip for a shared clock. **Correctness now depends on time synchronisation across the realm**, which is why a Kerberos deployment runs the Network Time Protocol (NTP) and why "clock skew too great" is a routine operational failure rather than a corner case. RFC 4120 also splits the KDC into an Authentication Server, which issues a Ticket-Granting Ticket, and a Ticket-Granting Server, which issues per-service tickets against that TGT — so a principal authenticates with its long-term key once and obtains tickets for many services afterwards.

### Implementation sketch (Scala)

The load-bearing check is the ticket predicate, not the cryptography. Decryption is elided; what follows is the acceptance logic a service performs on a presented ticket and authenticator.

```scala
import java.time.{Duration, Instant}
import scala.collection.mutable

final case class Ticket(client: String, sessionKey: Array[Byte],
                        issuedAt: Instant, lifetime: Duration)

final case class Authenticator(client: String, sentAt: Instant)

enum Verdict:
  case Accept
  case Reject(reason: String)

final class ServiceEndpoint(skew: Duration = Duration.ofMinutes(5)):
  // Replay cache, keyed by (client, timestamp). Eviction of entries older than
  // the skew window is elided; without it this set grows without bound.
  private val seen = mutable.Set.empty[(String, Instant)]

  def verify(t: Ticket, a: Authenticator, now: Instant): Verdict =
    if a.client != t.client then Verdict.Reject("client mismatch")
    else if now.isAfter(t.issuedAt.plus(t.lifetime)) then Verdict.Reject("ticket expired")
    else if Duration.between(a.sentAt, now).abs.compareTo(skew) > 0 then
      Verdict.Reject("clock skew too great")
    else if !seen.add((a.client, a.sentAt)) then Verdict.Reject("replayed authenticator")
    else Verdict.Accept
```

Removing the `issuedAt`/`lifetime` test reduces the endpoint to Needham–Schroeder's message 3: a ticket with a recoverable key remains valid indefinitely.

## Why the shape recurs

A trusted issuer mints a short-lived, cryptographically protected token that a resource server validates locally, without a callback to the issuer. A Kerberos ticket carrying a timestamp and lifetime is structurally the same object as a JSON Web Token (JWT) carrying `iat` and `exp` claims: expiry substitutes for an interactive freshness proof, and the substitution is only sound while the validator's clock is trustworthy.

## Pitfalls

- **A ticket without an expiry is a permanent credential.** Any later recovery of the session key reinstates the ticket's authority, because the verifier has no field on which to reject it.
- **Timestamps alone do not stop replay within the skew window.** A ticket and authenticator captured and re-sent inside the accepted window verify successfully unless the service keeps a replay cache of authenticators seen during that window.
- **Clock drift presents as an authentication failure, not a time failure.** A host whose clock has drifted beyond the skew window is rejected by every service in the realm, and the reported error names the credential rather than the clock.
- **A verbatim echo would not authenticate.** Returning `N_b` unchanged in message 5 proves nothing, since the value was transmitted by Bob; the transform to `N_b − 1` is what requires possession of `K_AB`.
- **Message 2 without Bob's name is exploitable.** Omitting the peer identity from the KDC's reply lets an attacker who rewrites message 1 obtain a session key for a different principal while Alice believes she is talking to Bob.
