---
title: "Zanzibar-style authorization: relationship tuples, zookies, and the \"new enemy\" problem"
date: 2026-08-13
track: sys-patterns
summary: "Google's Zanzibar (USENIX ATC 2019) models permissions as a graph of relation tuples, evaluates them with userset rewrites, and answers trillions of ACLs at millions of checks per second under 10 ms — while zookies address the consistency bug where a stale ACL exposes a new document to an old enemy. SpiceDB and OpenFGA are the open-source descendants."
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

**Gist.** A single product can answer "may Alice view this document?" with a local predicate; a fleet of products cannot, because the answer depends on group membership, folder inheritance and role hierarchies that live in other systems. [Zanzibar](https://www.usenix.org/conference/atc19/presentation/pang) stores every permission as a uniform relation tuple and evaluates a check as **reachability over the graph those tuples induce**, extended by rewrite rules declared per object type. The cost is that authorization becomes a distributed-systems component with a replication and caching layer, and correctness then requires an explicit consistency token — the zookie — to prevent a check from being answered against an access-control list (ACL) older than the content it protects.

Zanzibar is Google's unified authorization service, reported in the ATC 2019 paper as serving trillions of access-control tuples and millions of checks per second at a 95th-percentile latency under 10 ms with five-nines availability. It is the reference design for **relationship-based access control (ReBAC)**.

## The data model: relation tuples

All permissions are rows of one shape: `object#relation@user`, for example `doc:readme#owner@user:alice`. The `user` slot may instead hold a **userset** — another object's relation — as in `doc:readme#viewer@group:eng#member`, read as "members of group eng are viewers of readme". **That single indirection is what turns a table into a graph**: groups nest, folders contain documents, and a check becomes a traversal rather than a row lookup. Role-based access control ("Alice has role editor") and flat ACLs are both degenerate cases in which no tuple names a userset.

## Namespace configs and userset rewrites

Tuples alone would require materializing every implication, so that granting `owner` also wrote a `viewer` tuple. Instead each namespace (object type) carries a configuration of **userset rewrite** rules, evaluated at check time:

- `computed_userset` — `viewer` includes `editor`, `editor` includes `owner`: a role hierarchy expressed without additional tuples.
- `tuple_to_userset` — follow the `parent` tuple to a folder, then evaluate `viewer` *on that folder*: inheritance down a containment hierarchy.
- Union, intersection and exclusion combine the above into a rewrite expression per relation.

**Facts live in tuples; policy lives in the configuration.** Application teams write relationships; the rewrite rules determine what those relationships imply.

## The API: check, expand, read, write

`Check(object#relation@user, zookie)` returns a boolean by expanding the rewrite expression and following tuples — a recursive evaluation whose branches Zanzibar evaluates concurrently, short-circuits, and caches. `Expand` returns the effective userset *tree* for an `object#relation`, which answers not only who may see an object but through which edges. `Read` and `Write` manage tuples, with writes committed through Spanner. For the pathological shapes — deeply nested groups, groups with very wide membership — a separate indexing system called **Leopard** precomputes flattened group memberships, so that such a check consults the index rather than traversing the nesting at request time.

## The "new enemy" problem and zookies

The failure mode has a precise shape. Consider two writes issued in order: (1) Alice removes Bob from a document's viewers, then (2) Alice adds a paragraph Bob must not see. If a later check for Bob is evaluated against an ACL snapshot taken *before* (1) while the content server returns the version written by (2), Bob — the "new enemy" — reads new content under revoked access. **The bug is not staleness as such but the reordering of the ACL change relative to the content change.** Eventual consistency of the ACL store therefore does not suffice; requiring every check to read the latest ACL state at a global snapshot would remove the freedom to serve checks from replicas and caches, which is where the latency budget is met.

Zanzibar's mechanism is the **zookie**: an opaque consistency token encoding a timestamp. When content is saved, the application obtains a zookie and stores it **alongside the content version**. Every subsequent check on that version presents the zookie, and Zanzibar evaluates at a snapshot **at least as fresh as the timestamp the zookie encodes**. The invariant is thus: *the ACL snapshot used to guard a content version is never older than that content version*. Checks whose zookie is already covered by a replica's safe timestamp are served locally; only checks demanding a newer snapshot pay for freshness.

## Why authorization is a distributed-systems problem

Every request to every calling product performs a check before data is returned, so the authorization service absorbs the *sum* of its callers' traffic and sits inside each caller's latency budget: it must be both faster and more available than anything that depends on it. Caching is therefore mandatory, yet revocation is exactly the case in which a stale cache is a security defect — which is what the zookie makes safe to cache against. Hot objects checked repeatedly, fan-out over nested groups, and global replication complete the picture.

### Implementation sketch (Scala)

The load-bearing part of `Check` is the rewrite interpreter: `Union`/`Intersection`/`Exclusion` over `This`, `ComputedUserset` and `TupleToUserset`, with the graph walked lazily and cycles cut by a visited set.

```scala
final case class Obj(ns: String, id: String)
final case class Userset(obj: Obj, relation: String)     // object#relation
enum Subject:                                            // the @user slot
  case User(id: String)
  case Set(us: Userset)                                  // e.g. group:eng#member

enum Rewrite:
  case This                                              // tuples stored directly
  case Computed(relation: String)                        // computed_userset
  case Via(tupleset: String, computed: String)           // tuple_to_userset
  case Union(of: List[Rewrite])
  case Intersect(of: List[Rewrite])
  case Exclude(base: Rewrite, minus: Rewrite)

trait Store:
  def config(ns: String, relation: String): Rewrite
  def subjects(us: Userset): List[Subject]               // one tuple hop

def check(store: Store, us: Userset, who: String, seen: Set[Userset] = Set.empty): Boolean =
  if seen(us) then false                                 // cycle: no new evidence
  else
    def eval(r: Rewrite): Boolean = r match
      case Rewrite.This =>
        store.subjects(us).exists:
          case Subject.User(id) => id == who
          case Subject.Set(inner) => check(store, inner, who, seen + us)
      case Rewrite.Computed(rel) => check(store, us.copy(relation = rel), who, seen + us)
      case Rewrite.Via(tupleset, computed) =>
        store.subjects(us.copy(relation = tupleset)).exists:
          case Subject.Set(parent) => check(store, Userset(parent.obj, computed), who, seen + us)
          case Subject.User(_) => false
      case Rewrite.Union(of) => of.exists(eval)
      case Rewrite.Intersect(of) => of.forall(eval)
      case Rewrite.Exclude(b, m) => eval(b) && !eval(m)
    eval(store.config(us.obj.ns, us.relation))
```

## Open-source Zanzibar: SpiceDB and OpenFGA

Two production-grade descendants:

- **[SpiceDB](https://authzed.com/docs/spicedb/concepts/schema)** (AuthZed) — carries the zookie forward as the **ZedToken**; backed by Postgres, CockroachDB or Spanner.
- **[OpenFGA](https://openfga.dev/docs/concepts)** (started at Auth0/Okta) — a [CNCF Incubating project since November 2025](https://www.cncf.io/blog/2025/11/11/openfga-becomes-a-cncf-incubating-project/); a domain-specific modelling language, with HTTP and gRPC Check, ListObjects and ListUsers APIs.

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

`parent->view` is the paper's `tuple_to_userset`; `viewer + edit` is a `computed_userset` union. The same model in OpenFGA reads `define view: viewer or edit or view from parent`, and the Check API returns `{"allowed": true}`.

## Pitfalls

- **Storing the zookie separately from the content version defeats it.** If the zookie is kept per document rather than per revision, a check on an old revision is pinned to a newer snapshot than needed, or worse, a new revision inherits an older token and the new-enemy window reopens.
- **Requesting full consistency on every check erases the replica tier.** Each check then waits on a fresh snapshot instead of being served from bounded-stale state, and the service's latency ceases to fit inside its callers' budgets.
- **Wide or deeply nested groups make check latency depend on membership size.** This is the shape Leopard's flattened closures exist to absorb; without an equivalent index, a single viral object's group expansion dominates the tail.
- **Encoding policy as tuples instead of rewrites forces backfills.** Materializing "owner implies viewer" as extra tuples means every change to the role hierarchy rewrites history rather than editing one configuration.
- **Cycles in userset references cause non-termination in a naive evaluator.** A `group:a#member@group:b#member` pair with the reverse edge recurses forever unless the traversal carries a visited set.
- **Deleting a tuple is a security-relevant write, not a cleanup.** Any cache or replica that may still answer from a pre-deletion snapshot will keep granting access until the check's required snapshot advances past the revocation.
