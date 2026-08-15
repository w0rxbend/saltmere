---
title: "Dynamic Secrets with OpenBao: Short-Lived Credentials from the Open-Source Vault Fork"
date: 2026-08-15
track: microservices
summary: "Static database passwords in environment variables are valid indefinitely, shared across replicas, and unrotatable in practice. OpenBao — the Linux Foundation fork created after HashiCorp moved Vault to the Business Source License in August 2023, now on the 2.6 line — mints per-service, per-lease credentials with a time-to-live. This article walks the dynamic-credentials workflow end to end: database secrets engine, key-value v2, transit encryption, and Kubernetes authentication, with the bao CLI commands."
reading_time: 6
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

**Gist.** A database password held in an environment variable has three properties that no amount of careful handling removes: it is **valid until someone changes it**, it is **shared by every replica** and by anyone who can read a pod description, and changing it requires a coordinated redeploy. OpenBao replaces such a secret with a **dynamic credential**: it holds one privileged database account, creates a fresh role on demand for each consumer, and drops that role when the lease expires. The cost is a new hard dependency — the credential broker sits in front of the database, so its unavailability becomes a database outage once leases run out.

## Origin of the fork

HashiCorp Vault defined this category. In **August 2023** HashiCorp relicensed Vault (and Terraform) from the Mozilla Public License 2.0 (MPL 2.0) to the **Business Source License** (BSL) — source-available, with field-of-use restrictions on parties deemed competitive with HashiCorp. Two forks followed: OpenTofu for Terraform, and **OpenBao** for Vault, adopted by the **Linux Foundation** (under LF Edge) and cut from Vault's last MPL-licensed code. The first general-availability release, 2.0, appeared in 2024.

The current line is **2.6**. Features shipped in OpenBao that Vault's open tier did not carry include **namespaces** for multi-tenancy, available in Vault only in its Enterprise tier; horizontal read scalability on standby nodes (2.5); and, in 2.6, **namespace sealing**, auto-unseal through external key-management-service (KMS) plugins, and distroless container images. The command-line interface is `bao`. For the application programming interface (API) surface inherited at the fork it remains compatible with Vault's, so Vault client libraries generally work when pointed at a different address; divergence grows with each release on either side.

## Dynamic database credentials

The mechanism is delegation of account creation. OpenBao is configured with a **single privileged database account** and a set of role templates. When a client reads `database/creds/<role>`, OpenBao executes that role's `creation_statements` against the database — issuing `CREATE ROLE` with a generated name and password — and records a **lease** with a time-to-live (TTL). At lease expiry or on explicit revocation, OpenBao executes the corresponding revocation and the role ceases to exist. **No long-lived credential ever reaches the application.**

{% raw %}
```bash
bao server -dev                      # dev mode: in-memory, auto-unsealed. Never production.

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

Two invariants are worth naming. First, `bao write -f database/rotate-root/appdb` rotates the privileged account's password after configuration, so **the value written in the command above stops being valid and is known to no human**. Second, containment of a leaked dynamic credential has two independent paths: the credential expires on its own at `default_ttl`, bounded above by `max_ttl`; and `bao lease revoke -prefix database/creds/orders-svc` terminates every outstanding lease under that prefix immediately.

The {%- raw -%} `VALID UNTIL '{{expiration}}'` {%- endraw -%} clause matters because it makes the database itself enforce the deadline. Without it, expiry depends entirely on OpenBao successfully running the revocation statement; with it, **the database refuses the login after the timestamp even if revocation never runs**.

## Key-value v2 and transit

Some material cannot be dynamic — a third-party API key is issued by a party that does not offer on-demand minting. The **key-value version 2 (KV v2)** engine stores such values with versioning, soft deletion, and check-and-set semantics:

```bash
bao secrets enable -version=2 kv
bao kv put kv/orders/stripe api_key=sk_live_...
bao kv get kv/orders/stripe                   # latest version
bao kv get -version=1 kv/orders/stripe        # an earlier version, still retrievable
```

The **transit** engine inverts the direction of data flow: OpenBao retains the key material and applications send plaintext or ciphertext to it over the API — encryption as a service. Applications never hold key material. Rotation via `bao write -f transit/keys/orders/rotate` requires no application change because **each ciphertext carries the version of the key that produced it**, so previously written values remain decryptable until the key's minimum decryption version is raised past them.

```bash
bao secrets enable transit
bao write transit/encrypt/orders plaintext=$(echo -n '4111...' | base64)
# ciphertext is returned with a version marker — the v1 records which key version encrypted it
```

## Kubernetes authentication and the bootstrap problem

Dynamic secrets create a recursion: a service must authenticate to OpenBao before it can obtain credentials. The **Kubernetes auth method** resolves it by consuming an identity the pod already possesses — its projected **service account (SA) token**:

1. `bao auth enable kubernetes`, configured with the cluster API address and certificate authority.
2. A role binds `serviceaccount=orders-svc` in `namespace=prod` to a set of policies and a token TTL.
3. The pod POSTs its projected SA JSON Web Token (JWT) to `auth/kubernetes/login`; OpenBao validates it against the cluster's **TokenReview API** and returns a scoped token.
4. The service — or an init container, or the Bao agent sidecar — uses that token to read `database/creds/orders-svc` and renews the lease for as long as the pod lives.

The trust chain terminates at the Kubernetes API server: **OpenBao issues no credential that the cluster would not itself vouch for**.

| | Static secret in env/ConfigMap | OpenBao dynamic credential |
|---|---|---|
| **Lifetime** | Unbounded | TTL (e.g. 1h), auto-revoked |
| **Blast radius** | Every replica, every environment dump | One lease, one workload |
| **Rotation** | Manual, needs redeploy | Continuous by construction |
| **Revocation** | Change password everywhere | `bao lease revoke`, immediate |
| **Audit** | None (holders unknowable) | Every issuance in the audit log |

### Implementation sketch (Scala)

The client-side obligation is a small state machine: hold a lease, renew before expiry, and re-acquire a fresh credential when renewal fails or `max_ttl` is reached. The sketch below models that loop; the HTTP calls to `/v1/database/creds/...` and `/v1/sys/leases/renew` are elided.

```scala
final case class Lease(leaseId: String, username: String, password: String, ttl: FiniteDuration)

trait Broker:
  def issue(role: String): Lease
  def renew(leaseId: String, increment: FiniteDuration): Option[Lease] // None once max_ttl is hit

final class CredentialHolder(broker: Broker, role: String):
  private val current = new AtomicReference[Lease](broker.issue(role))

  def credential: Lease = current.get()

  // Runs forever on its own thread. Renewal is attempted at a fraction of the
  // TTL so a failed renewal still leaves time for a full re-issue before the
  // database rejects the login.
  def maintain(): Unit =
    while true do
      val lease = current.get()
      Thread.sleep((lease.ttl * 2 / 3).toMillis)
      current.set(broker.renew(lease.leaseId, lease.ttl).getOrElse(broker.issue(role)))
```

The load-bearing property is that **renewal failure is not an error path but the normal path**: `max_ttl` guarantees every lease eventually refuses renewal, so re-issue must be as well-tested as the happy case. A connection pool holding the old username survives until its sockets are recycled, so the pool must be rebuilt — or configured to open new connections with the new credential — when `current` changes.

## Pitfalls

- **Leaving the configured root database password in place.** Without `database/rotate-root`, the privileged account's password remains in shell history, configuration management, and the operator's memory, and it can create arbitrary roles.
- **Omitting {%- raw -%} `VALID UNTIL '{{expiration}}'` {%- endraw -%} in `creation_statements`.** If OpenBao cannot reach the database at revocation time, the role persists with a password that has already left the lease's protection window.
- **`max_ttl` reached while the pod is still running.** Renewal stops succeeding and the application must re-read `database/creds/...`; code that only renews and never re-issues loses its database at a predictable, and therefore synchronised, moment across replicas.
- **Connection pools pinned to an expired credential.** The pool holds open sockets authenticated with a dropped role; existing connections keep working until one is recycled, at which point reconnection fails with an authentication error rather than a lease error, which misdirects diagnosis.
- **Role churn in the database's catalogue.** Every issuance creates a role; short TTLs with many replicas produce a large number of short-lived roles, and failed revocations accumulate rather than disappear.
- **Treating the broker as non-critical infrastructure.** Once leases expire, an unavailable OpenBao is indistinguishable from an unavailable database. Raft integrated storage with multiple standbys, auto-unseal through a KMS plugin, and the 2.5 horizontal read scaling address availability; single-node deployments do not.
- **Assuming a dev-mode server resembles production.** `bao server -dev` is in-memory and auto-unsealed; nothing about seal handling, storage durability, or unseal procedure is exercised by it.
