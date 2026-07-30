---
title: "Needham–Schroeder, the replay bug, and why Kerberos adds a clock"
date: 2026-07-30
track: distributed-systems
summary: "The Needham–Schroeder symmetric-key protocol is the ancestor of Kerberos: a trusted server hands out session keys so two parties who share nothing can authenticate. It also had a famous replay flaw that took 17 years to publish. Here's the message flow, the attack, and the timestamp fix Kerberos uses."
reading_time: 6
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

Two machines that have never met, share no secret, and don't trust the network between them — how do they end up with a shared session key that each is sure the other holds? The classic answer is a *trusted third party*, and the classic protocol is Needham–Schroeder (1978). If you've ever used Kerberos, you've used its direct descendant.

## The setup

Alice (A) and Bob (B) each share a long-term symmetric key with a Key Distribution Center (KDC): `K_A` and `K_B`. Nobody else knows those. The KDC's job is to mint a fresh *session key* `K_AB` and get it to both parties, encrypted so only they can read it. The protocol uses **nonces** — one-time random numbers — to prove freshness. Notation: `{X}K` means "X encrypted under key K".

```
1. A -> KDC:  A, B, N_a
2. KDC -> A:  {N_a, B, K_AB, {K_AB, A}K_B}K_A
3. A -> B:    {K_AB, A}K_B
4. B -> A:    {N_b}K_AB
5. A -> B:    {N_b - 1}K_AB
```

Read it slowly, because every field is load-bearing:

- **Message 1**: Alice tells the KDC she wants to talk to Bob, and includes a fresh nonce `N_a`.
- **Message 2**: The KDC replies, encrypted under `K_A` so only Alice can open it. Inside: her nonce `N_a` (proves this reply is fresh and really answers *her* request, not a replay), Bob's name (so an attacker can't swap in a different party), the new session key `K_AB`, and a sealed *ticket* `{K_AB, A}K_B` that Alice can't read but can forward.
- **Message 3**: Alice hands Bob the ticket. Bob decrypts it with `K_B`, learns the session key and that his peer claims to be Alice.
- **Messages 4–5**: A challenge-response so Bob knows Alice is *live*. Bob sends a nonce `N_b` encrypted under the session key; Alice returns `N_b − 1`. Only someone holding `K_AB` could have done that transform, so Bob is convinced.

## The replay flaw

Denning and Sacco pointed out the hole in 1981, three years after publication: **message 3 has no freshness guarantee for Bob.** The ticket `{K_AB, A}K_B` never expires. If an attacker ever recovers an old session key `K_AB` (offline cracking, a compromised endpoint, whatever), they can replay message 3 to Bob forever, and Bob will happily complete steps 4–5 believing he's talking to Alice. Alice was long ago proven fresh by her nonce in message 2; Bob was never given the same protection.

The elegance of the bug is that the nonces *do* protect Alice — the protocol is asymmetric in who gets a freshness guarantee, and the ticket is the unprotected leg.

## Two ways to fix it

**Needham & Schroeder's own fix (1987)** added another round so Bob contributes a nonce *before* the ticket is minted, binding the ticket to something fresh from Bob's side.

**Kerberos's fix** is the one you actually run: put a **timestamp and lifetime inside the ticket**. Instead of proving freshness with an extra round trip, Kerberos tickets carry `{K_AB, A, timestamp, lifetime}K_B`. Bob checks that the timestamp is recent (within a clock-skew window, typically 5 minutes) and rejects anything older. A replayed ticket is simply *expired*. The client also sends an *authenticator* — a timestamp encrypted under the session key — so Bob knows the sender holds `K_AB` *now*, not just once upon a time.

The tradeoff Kerberos makes explicit: replacing nonces with timestamps means **you now depend on loosely synchronized clocks.** That's why a Kerberos realm needs NTP and why "clock skew too great" is the error every sysadmin who's run Active Directory or MIT Kerberos has seen. RFC 4120 formalizes all of this — the KDC splits into an Authentication Server (issues a Ticket-Granting Ticket) and a Ticket-Granting Server (issues per-service tickets), so you authenticate once and get tickets for many services without re-sending your password.

## Why this still matters

The pattern — a trusted issuer mints a short-lived, signed/encrypted token that a resource server can validate *without calling back to the issuer* — is exactly the shape of modern token auth. A Kerberos ticket with a timestamp and lifetime is structurally a JWT with `iat`/`exp`. The 1978 replay bug and its timestamp fix are the reason your JWTs have expiry claims and your infra runs NTP.

**Try next:** Sketch the five messages on paper, then trace the Denning–Sacco replay: cross out messages 1 and 2, hand the attacker a stale `K_AB` and ticket, and confirm Bob has no field to reject it — then add `timestamp, lifetime` to the ticket and watch the replay die. That single field is the whole difference between Needham–Schroeder and Kerberos.
