---
title: "Dynamic Secrets with OpenBao: Short-Lived Credentials from the Open-Source Vault Fork"
date: 2026-08-15
track: microservices
summary: "Static database passwords in env vars are forever-valid, shared, and unrotatable in practice. OpenBao — the Linux Foundation fork created after HashiCorp moved Vault to the BSL in August 2023, now at 2.6 (July 2026) — fixes this by minting per-service, per-lease credentials with a TTL. Here is the dynamic-credentials workflow end to end: database secrets engine, KV v2, transit encryption, and Kubernetes auth, with the actual bao CLI commands."
reading_time: 5
tags: [openbao, vault, secrets-management, dynamic-credentials, kubernetes, security]
sources:
  - title: "OpenBao 2.6.x release notes — OpenBao"
    url: "https://openbao.org/community/release-notes/2-6-0/"
  - title: "Database secrets engine — OpenBao Docs"
    url: "https://openbao.org/docs/secrets/databases/"
  - title: "Kubernetes auth method — OpenBao Docs"
    url: "https://openbao.org/docs/auth/kubernetes/"
  - title: "HashiCorp adopts Business Source License — HashiCorp Blog (Aug 2023)"
    url: "https://www.hashicorp.com/blog/hashicorp-adopts-business-source-license"
  - title: "OpenBao — endoflife.date (release/support timeline)"
    url: "https://endoflife.date/openbao"
---

A database password in an environment variable has three permanent properties: it is **valid forever**, it is **shared** by every replica (and every engineer who ever ran `kubectl describe pod`), and rotating it means a coordinated redeploy — so nobody rotates it. When it leaks, you find out from your AWS bill. The fix is not better hiding places for static secrets; it is secrets that are **dynamic**: generated on demand, scoped to one consumer, and dead in an hour.

## Why OpenBao exists

HashiCorp Vault defined this category. In **August 2023** HashiCorp moved Vault (and Terraform) from MPL 2.0 to the **Business Source License** — source-available, but with field-of-use restrictions on anyone "competitive" with HashiCorp. The community response was two forks: OpenTofu for Terraform, and **OpenBao** for Vault, adopted by the **Linux Foundation** (under LF Edge) and cut from Vault's last MPL-licensed code. Its first GA release, 2.0, landed in July 2024.

By August 2026 OpenBao is well past "the fork": the current line is **2.6** (2.6.0 released July 14, 2026; 2.6.1 on July 22), and it has been shipping features Vault's open tier never had — **namespaces** (multi-tenancy, previously Vault Enterprise-only), horizontal read scalability on standby nodes (2.5, Feb 2026), and in 2.6: **namespace sealing** (per-tenant cryptographic key material, so one tenant can be revoked without touching others), auto-unseal via external **KMS plugins**, authenticated root-token generation, and distroless container images. The CLI is `bao`, API-compatible with Vault's, so most Vault client libraries work by pointing them at a different address.

## Dynamic database credentials

The core trick: OpenBao holds one privileged DB account and uses it to `CREATE ROLE` on demand. Each service login gets a **fresh username/password pair with a TTL**; when the lease expires, OpenBao drops the role. Nothing long-lived ever reaches the application.

{% raw %}
```bash
bao server -dev                      # dev mode: in-memory, auto-unsealed. Never prod.

bao secrets enable database
bao write database/config/appdb \
    plugin_name=postgresql-database-plugin \
    connection_url="postgresql://{{username}}:{{password}}@pg:5432/appdb" \
    username="bao-root" password="rotate-me-immediately" \
    allowed_roles="orders-svc"

bao write database/roles/orders-svc \
    db_name=appdb \
    creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' \
      VALID UNTIL '{{expiration}}'; GRANT SELECT, INSERT ON orders TO \"{{name}}\";" \
    default_ttl=1h max_ttl=24h

bao read database/creds/orders-svc
# username  v-kubernet-orders-svc-x7Qw...   password  A1b2...   lease_duration  1h
```
{% endraw %}

Two operational notes. First, run `bao write -f database/rotate-root/appdb` after config — OpenBao rotates the root DB password so even *you* no longer know it. Second, a leaked dynamic credential is contained twice over: it expires on its own, and `bao lease revoke -prefix database/creds/orders-svc` kills every outstanding lease now.

## KV v2 and transit

Not everything can be dynamic — third-party API keys are static by nature. **KV v2** stores them versioned, with rollback and check-and-set:

```bash
bao secrets enable -version=2 kv
bao kv put kv/orders/stripe api_key=sk_live_...
bao kv get -version=2 kv/orders/stripe        # old versions retrievable, deletions soft
```

The **transit** engine is the inverse service: OpenBao holds encryption keys and your services send data to be encrypted/decrypted over the API — "encryption as a service." Applications never see key material, and key rotation (`bao write -f transit/keys/orders/rotate`) requires no app change since ciphertexts carry a key version.

```bash
bao secrets enable transit
bao write transit/encrypt/orders plaintext=$(echo -n '4111...' | base64)
# ciphertext: vault:v1:8SDd3WHDOjf7mq...
```

## Kubernetes auth: no secret zero

Dynamic secrets pose a bootstrap riddle: the service must authenticate to OpenBao to fetch credentials — with what? In Kubernetes, with the identity it already has. The **Kubernetes auth method** trades the pod's projected **service account token** for an OpenBao token:

1. `bao auth enable kubernetes`, configured with the cluster's API address and CA.
2. A role maps `serviceaccount=orders-svc` in `namespace=prod` to OpenBao policies and a token TTL.
3. The pod POSTs its projected SA JWT to `auth/kubernetes/login`; OpenBao validates it against the cluster's **TokenReview API**, returns a scoped token.
4. The service (or an init container / the Bao agent sidecar) uses that token to read `database/creds/orders-svc` and renews the lease until the pod dies.

| | Static secret in env/ConfigMap | OpenBao dynamic credential |
|---|---|---|
| **Lifetime** | Unbounded | TTL (e.g. 1h), auto-revoked |
| **Blast radius** | Every replica, every env dump | One lease, one workload |
| **Rotation** | Manual, needs redeploy | Continuous by construction |
| **Revocation** | Change password everywhere | `bao lease revoke`, instant |
| **Audit** | None ("who has it" unknowable) | Every issuance in the audit log |

The honest caveat: OpenBao becomes availability-critical — if it is down and leases expire, services lose their databases. That is what the 2.5 horizontal read scaling and proper HA (Raft integrated storage, multiple standbys, auto-unseal via KMS) exist for. Treat it like you treat your database, because now it stands in front of your database.

**Try next:** run `bao server -dev` against a local Postgres container, wire up the database engine as above, then watch `\du` in psql while the lease TTL expires — the role appears and vanishes on its own.
