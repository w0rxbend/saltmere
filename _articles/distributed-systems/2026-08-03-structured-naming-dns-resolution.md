---
title: "Structured naming, and DNS resolution one delegation at a time"
date: 2026-08-03
track: distributed-systems
summary: "Van Steen & Tanenbaum model a name space as a labeled graph walked from a known closure point. The Domain Name System (DNS) is that model made concrete: a hierarchy of zones resolved by chasing referrals from the root. This article maps the textbook model onto the output of `dig +trace`, line by line."
reading_time: 7
tags: [naming, dns, resolution, closure, ttl, zones, distributed-systems]
sources:
  - title: "Van Steen & Tanenbaum — Distributed Systems (4th ed.), Chapter 5: Naming"
    url: "https://www.distributed-systems.net/index.php/books/ds4/"
  - title: "RFC 1034 — Domain Names: Concepts and Facilities"
    url: "https://www.rfc-editor.org/rfc/rfc1034.html"
  - title: "RFC 1035 — Domain Names: Implementation and Specification"
    url: "https://www.rfc-editor.org/rfc/rfc1035.html"
  - title: "ISC Knowledge Base — dig and the +trace option"
    url: "https://kb.isc.org/docs/aa-00208"
  - title: "Cloudflare Learning Center — DNS server types"
    url: "https://www.cloudflare.com/learning/dns/dns-server-types/"
---

**Gist.** A name space large enough to span administrative boundaries cannot be resolved by one table lookup, because no single authority holds the whole table. Structured naming solves this by modelling the space as a labeled directed graph partitioned into independently administered subgraphs, and resolving a name as a walk that crosses one partition boundary per step, starting from an out-of-band **closure point**. The cost is that every lookup becomes a multi-round-trip chain of network queries against machines under different control, which is paid down only by caching each intermediate step under a time-to-live (TTL) — and therefore by tolerating stale answers for the length of that TTL.

## The name space is a graph, and resolution requires a closure point

Van Steen and Tanenbaum's Naming chapter builds structured naming from one abstraction: a **name space is a labeled, directed graph**. Interior nodes are *directory nodes*, each holding a table of (edge label → node) pairs; leaf nodes hold the entity being named. A *path name* such as `/home/steen/mbox` is the sequence of edge labels followed from a starting node. Resolution then reduces to two questions: where the walk starts, and how it advances from a known node to the target.

The difficult half is the start, not the walk. Resolving `/home/steen/mbox` presupposes knowledge of which node `/` denotes and how to contact it. The textbook calls this the **closure mechanism**: name resolution always assumes an implicit, out-of-band starting point, since otherwise a name would be needed to locate the mechanism that resolves names, without termination. **The closure point is not itself nameable within the system.** In a filesystem it is the inode of the root directory, hard-wired into the kernel. In DNS it is the **root hints file**: a small, statically shipped list of the root servers' addresses that every resolver possesses before it resolves anything. That is the single piece of information resolution cannot bootstrap for itself.

DNS names read right-to-left as a path from the root: `www.example.com.` is `root → com → example → www`. The trailing dot denotes the root — normally elided, but it is the closure point made visible. Each edge label (`com`, `example`, `www`) is a lookup in a directory node, and **the directory nodes are distributed across the internet on machines under different administrative control**. That distribution is what makes resolution a distributed-systems problem rather than a data-structure problem.

## Iterative versus recursive: which party holds the work

The textbook's sharpest distinction concerns which party carries the outstanding work.

In **iterative resolution**, the client asks a server for a name; if that server is not authoritative for it, the server does not chase the answer. It returns a *referral*: the address of a server one level closer to the target. The client queries that server next, and repeats. **The client does the walking; each server answers only for the edge it owns.**

In **recursive resolution**, the client hands the full name to one server, which queries the next server on the client's behalf, possibly transitively, and the final answer propagates back along the chain. **The client does no walking; one server absorbs the latency and holds the pending state.**

Deployed DNS is a hybrid, and the split explains the rest of the design:

| | Stub → recursive resolver | Recursive resolver → root/TLD/authoritative |
|---|---|---|
| Mode | **recursive** (full answer requested) | **iterative** (follows referrals itself) |
| Who walks | the resolver (e.g. `1.1.1.1`, `8.8.8.8`) | the resolver, one delegation at a time |
| Consequence | one round trip for the client | root and top-level-domain (TLD) servers refer only |

Root and TLD servers answering iteratively perform **bounded work per query and hold no per-client state**: a referral is a fixed-size response assembled from the zone's own delegation records, with no dependency on any other server's availability. A recursive server, by contrast, must retain the pending query while it waits on upstream responses. The multi-step walk and the cache both live in the recursive resolver.

## Observing the walk with `dig +trace`

An ordinary resolver conceals the walk, answering from cache in one hop. `dig +trace` disables that path. As ISC documents the option, dig makes its own iterative queries starting at the root servers, follows each delegation referral it receives, and prints the response at every step, rather than delegating the whole question to one upstream resolver:

```
$ dig +trace www.example.com

; <<>> DiG 9.18 <<>> +trace www.example.com

.            518400  IN  NS  a.root-servers.net.      # (1) from root hints (closure):
.            518400  IN  NS  b.root-servers.net.      #     the root servers, TTL 6 days
                     ...
com.         172800  IN  NS  a.gtld-servers.net.      # (2) root REFERS down to .com
com.         172800  IN  NS  b.gtld-servers.net.
;; Received 1174 bytes from 198.41.0.4#53(a.root-servers.net)   # <- queried a root server

example.com. 172800  IN  NS  a.iana-servers.net.      # (3) .com REFERS to example.com's
example.com. 172800  IN  NS  b.iana-servers.net.      #     authoritative servers
;; Received 771 bytes from 192.5.6.30#53(a.gtld-servers.net)    # <- queried a .com TLD server

www.example.com. 86400 IN A  23.215.0.136             # (4) AUTHORITATIVE answer, no referral
;; Received 56 bytes from 199.43.135.53#53(a.iana-servers.net)  #     the leaf node is reached
```

The output is a graph walk. Line (1) is the **closure point**: the trace begins from the root hints, the one datum that was not looked up. Each `;; Received ... from` line names the directory node that answered. Lines (2) and (3) are **referrals** — `NS` records with no `A` record for `www` — each server returning the node one label down instead of the final answer. Line (4) carries an `A` record with no accompanying referral, from `a.iana-servers.net`, the server **authoritative** for the `example.com` zone. **Absence of a referral is the termination condition of the walk.** The address in an `A` record can change between traces; the structure of the trace, not the address, is the stable part.

Two mechanisms deserve attention. First, **the `NS` record sets are the directory-node contents of the textbook model** — the edge table naming which node owns the next label. Second, when a referral names a nameserver whose address is not otherwise obtainable without resolving a name inside the zone being delegated, the parent zone includes **glue records**, the A/AAAA addresses of the child's nameservers, in the same response. Without glue the resolver would have to resolve `a.gtld-servers.net` in order to ask a question it needs answered in order to resolve `a.gtld-servers.net`; RFC 1034 specifies that a parent zone carry such glue in its delegations. Where glue is absent, `dig +trace` falls back to the resolver configured in `/etc/resolv.conf` to obtain the nameserver's address, so the trace is not purely self-contained.

## Name-space implementation: zones, not nodes

A large name space is not implemented node by node. It is *partitioned* into contiguous subgraphs, each managed by one authority, across three layers: a **global layer** (the root and TLDs, rarely changing, heavily replicated), an **administrational layer** (organizational domains such as `example.com`), and a **managerial layer** (the frequently changing leaf records). DNS calls each managed partition a **zone**. **The boundary between a zone and its child zone is exactly a delegation** — the `NS` referral on lines (2) and (3). `com` is one zone; `example.com` is a different zone delegated from it. Every referral in the trace crosses a zone boundary.

The layers differ in write rate, and the TTLs in the trace track that difference: global-layer `NS` records above carry 518400 s (6 days), while managerial-layer records typically carry far shorter values.

## Caching and TTL

A full iterative walk from the root on every lookup would place all query load on the root servers and add the latency of one round trip per label. DNS places the caching policy **in the data**: every record carries a **TTL**, defined in RFC 1035 as the interval, in seconds, for which the record may be cached before the source of the information should be consulted again. The recursive resolver caches each record independently — the `com` `NS` set, the `example.com` `NS` set, and the final `A` record. A subsequent query for any name under `.com` skips the root; a subsequent query for `www.example.com` within 86400 s is answered from cache without any network walk.

**TTL is the knob trading freshness against upstream load.** A long TTL (the root's 6 days) yields heavy caching and low query load, at the price of an equally long window during which a changed record is invisible to resolvers that already hold the old one. A short TTL propagates changes quickly and multiplies upstream queries. This is why `dig +trace` and a plain `dig www.example.com` differ in latency and sometimes in the returned address: the trace is a cold walk from closure with caching disabled, while the ordinary resolver answers warm. Same graph, same closure point, same algorithm — the difference is how much of the walk was already remembered.

### Implementation sketch (Scala)

The iterative loop is small: hold a current server set, ask, and either terminate on an authoritative answer or replace the server set from the referral. Glue is what keeps the loop from recursing into itself.

```scala
final case class Response(
    answers: List[String],      // A/AAAA records, non-empty ⇒ authoritative answer
    referral: List[String],     // NS names one label down
    glue: Map[String, String]   // NS name -> address, shipped by the parent zone
)

trait Wire:
  def query(serverAddr: String, name: String): Response

def resolveIteratively(
    wire: Wire,
    name: String,
    rootHints: List[String],          // the closure point: not itself resolvable
    maxDelegations: Int = 32
): Either[String, List[String]] =
  @annotation.tailrec
  def step(servers: List[String], depth: Int): Either[String, List[String]] =
    if depth > maxDelegations then Left("delegation loop or excessive depth")
    else servers.headOption match
      case None => Left("no reachable server for the next label")
      case Some(addr) =>
        val r = wire.query(addr, name)
        if r.answers.nonEmpty then Right(r.answers)          // answer records present ⇒ leaf reached
        else
          // Only glued nameservers can be followed without a nested resolution.
          val next = r.referral.flatMap(r.glue.get)
          if next.isEmpty then Left("referral without glue: nested resolution required")
          else step(next, depth + 1)

  step(rootHints, 0)
```

## Pitfalls

- **Treating `dig +trace` output as what the local resolver did.** The trace performs a cold walk with caching disabled; the configured resolver answers from cache and may return a far lower query time, and a different address where the name resolves to a rotating address set.
- **Assuming the trace is fully self-contained.** When a delegation lacks glue, `dig +trace` resolves the nameserver's name through `/etc/resolv.conf`, so part of the walk is performed by an upstream resolver rather than by dig.
- **Lowering a TTL immediately before a migration.** The reduction is itself subject to the *previous* TTL: resolvers holding the old record continue serving it, and the old value, until that older TTL expires.
- **Changing delegation `NS` records and expecting prompt effect.** Delegation records in the parent zone carry the parent's TTL (172800 s for `com` in the trace above), so resolvers keep querying the previous nameservers for that period.
- **Omitting glue for in-zone nameservers.** If `example.com`'s nameservers are named inside `example.com` and the parent ships no glue, a resolver must resolve a name in the zone it cannot yet reach; the query fails rather than looping.
- **Sending recursive queries to root or TLD servers.** Those servers return referrals only; a client expecting a final answer receives an `NS` set and must continue the walk itself.
