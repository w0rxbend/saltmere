---
title: "Zanzibar-style authorization: relationship tuples, zookies, and the \"new enemy\" problem"
date: 2026-08-13
track: sys-patterns
summary: "Google's Zanzibar (USENIX ATC 2019) models permissions as a graph of relation tuples, evaluates them with userset rewrites, and answers trillions of ACLs at millions of checks per second under 10 ms — while zookies solve the consistency bug where a stale ACL exposes a new document to an old enemy. SpiceDB and OpenFGA are the open-source descendants."
reading_time: 6
tags: [zanzibar, rebac, authorization, spicedb, openfga]
sources:
  - title: "Pang et al. — Zanzibar: Google's Consistent, Global Authorization System (USENIX ATC 2019)"
    url: "https://www.usenix.org/conference/atc19/presentation/pang"
  - title: "AuthZed — The Google Zanzibar Paper, annotated"
    url: "https://authzed.com/zanzibar"
  - title: "SpiceDB docs — Schema language (definition, relation, permission)"
    url: "https://authzed.com/docs/spicedb/concepts/schema"
  - title: "OpenFGA docs — Concepts (model DSL, tuples, Check API)"
    url: "https://openfga.dev/docs/concepts"
  - title: "CNCF — OpenFGA Becomes a CNCF Incubating Project (Nov 2025)"
    url: "https://www.cncf.io/blog/2025/11/11/openfga-becomes-a-cncf-incubating-project/"
---

"Can Alice view this doc?" looks like an `if` statement until you need it answered for Drive, YouTube, and Calendar, on every request, in single-digit milliseconds, globally consistently. [Zanzibar](https://www.usenix.org/conference/atc19/presentation/pang) is Google's answer — a unified, planet-scale authorization service serving trillions of access-control tuples and millions of checks per second at a 95th-percentile latency under 10 ms with five-nines availability. It's the reference design for **ReBAC** (relationship-based access control), and it turns authorization into a distributed-systems problem worth knowing cold.

## The data model: relation tuples

All permissions are rows of one shape: `object#relation@user`, e.g. `doc:readme#owner@user:alice`. The `user` slot can also be a **userset** — another object's relation — e.g. `doc:readme#viewer@group:eng#member`: "members of group eng are viewers of readme." That one indirection makes the model a graph: groups nest, folders contain docs, and a check becomes graph reachability, not a table lookup. RBAC ("Alice has role editor") and plain ACLs are both degenerate cases of this model.

## Namespace configs and userset rewrites

Tuples alone would force you to materialize every implication ("owners can also view"). Instead, each namespace (object type) carries a config with **userset rewrite** rules that compute effective usersets at check time:

- `computed_userset` — `viewer` includes `editor`, `editor` includes `owner` (role hierarchy without extra tuples).
- `tuple_to_userset` — follow the `parent` tuple to a folder, then evaluate `viewer` *there* (permission inheritance down a hierarchy).
- Union / intersection / exclusion combine them.

Policy lives in the config; facts live in tuples. That split is the whole trick — application teams write relationships, the rewrite rules decide what they mean.

## The API: check, expand, read, write

`Check(object#relation@user, zookie)` answers the boolean by walking rewrites and tuples — a recursive, fan-out evaluation that Zanzibar caches aggressively and short-circuits concurrently. `Expand` returns the effective userset tree for an `object#relation` (who can see this doc, and *why*). `Read`/`Write` manage tuples, with writes going through Spanner. For pathological cases — deeply nested, wide groups — a separate **Leopard** index precomputes flattened group closures so checks stay in budget.

## The "new enemy" problem and zookies

Here's the distributed-systems core. Suppose ACL changes and content changes are ordered inconsistently: (1) Alice removes Bob from a doc's viewers, then (2) adds a paragraph Bob must not see. If a check for Bob evaluates against an ACL snapshot from *before* step 1 while serving content from *after* step 2, Bob — the "new enemy" — reads the new content with revoked access. Plain eventual consistency of the ACL store is not safe; but forcing every check to read latest-everything would blow the latency budget.

Zanzibar's answer is the **zookie**: an opaque consistency token (a Spanner commit timestamp under the hood). When content is saved, the app asks Zanzibar for a zookie and stores it *with the content version*. Every later check on that version presents the zookie, and Zanzibar guarantees evaluation at a snapshot **at least as fresh** as it. Checks are thus causally pinned to the content they protect, while still being servable from bounded-stale replicas and caches almost everywhere — external consistency where it matters, cheap staleness where it doesn't.

## Why authz is a distributed-systems problem

Every request to every product pays an authorization check before returning data, so the authz service inherits the *sum* of its callers' traffic and sits inside everyone's latency budget — it must be faster and more available than any service that calls it. Caching is mandatory, but permission revocation is exactly the case where a stale cache is a security bug; zookies are what make caching safe. Add hot objects (a viral doc checked millions of times), fan-out on nested groups, and global replication, and you have latency-budget, cache-invalidation, and consistency problems — the reason this is a systems interview topic and not a policy-language one.

## Open-source Zanzibar: SpiceDB and OpenFGA

Two production-grade descendants, both actively maintained (versions checked August 2026):

- **[SpiceDB](https://authzed.com/docs/spicedb/concepts/schema)** (AuthZed) — the most faithful to the paper, including zookie-style **ZedTokens**; latest release v1.53.0 (May 2026); backed by Postgres/CockroachDB/Spanner.
- **[OpenFGA](https://openfga.dev/docs/concepts)** (started at Auth0/Okta) — a [CNCF Incubating project since November 2025](https://www.cncf.io/blog/2025/11/11/openfga-becomes-a-cncf-incubating-project/); latest release v1.17.1 (June 2026); DSL-based modeling, HTTP/gRPC Check, ListObjects and ListUsers APIs.

A SpiceDB schema with role hierarchy and folder inheritance, plus a check:

```zed
definition user {}

definition folder {
  relation viewer: user
  permission view = viewer
}

definition document {
  relation parent: folder
  relation owner:  user
  relation editor: user
  relation viewer: user
  permission edit = editor + owner
  permission view = viewer + edit + parent->view
}
```

```console
$ zed relationship create folder:plans viewer user:bob
$ zed relationship create document:roadmap parent folder:plans
$ zed permission check document:roadmap view user:bob
true    # via parent->view — no direct tuple on the document
```

`parent->view` is the paper's `tuple_to_userset`; `viewer + edit` is a `computed_userset` union. In OpenFGA the same model reads `define view: viewer or edit or view from parent`, and checks return `{"allowed": true}` from the Check API.

Interview summary: *tuples are facts, rewrites are policy, check is cached graph reachability, and the zookie pins each check to the content version it protects.*

**Try next:** run SpiceDB locally (`spicedb serve --grpc-preshared-key testkey`), load the schema above with `zed schema write`, and verify that deleting the `folder:plans viewer` tuple flips the inherited check to `false`.
