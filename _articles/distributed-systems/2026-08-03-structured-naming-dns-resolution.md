---
title: "Structured naming, and watching DNS resolve a name one delegation at a time"
date: 2026-08-03
track: distributed-systems
summary: "Van Steen & Tanenbaum model a name space as a labeled graph you walk from a known closure point. DNS is that model made concrete: a hierarchy of zones, resolved by chasing referrals from the root. This is how the textbook maps onto what `dig +trace` prints line by line."
reading_time: 6
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

Van Steen and Tanenbaum's Naming chapter builds up structured naming from one abstraction: a **name space** is a labeled, directed graph. Interior nodes are *directory nodes*, each holding a table of (edge label → node) pairs; leaf nodes hold the entity you actually wanted. A *path name* like `/home/steen/mbox` is just the sequence of edge labels you follow from some starting node. The whole theory of resolution then reduces to two questions: *where do I start walking*, and *how do I get from a node I know to the node I want*. DNS is the canonical implementation of exactly this graph, and the useful thing is that you can watch the walk happen with a single command. The textbook's abstract model and the bytes on the wire line up almost one-to-one.

## The name space is a graph, and resolution needs a closure point

The subtle part of resolution is not the walking — it's the starting. To resolve `/home/steen/mbox` you must already know which node `/` refers to and how to contact it. The textbook calls this the **closure mechanism**: name resolution always assumes an implicit, out-of-band starting point, because otherwise you'd need a name to find the thing that resolves names, forever. Closure is deliberately *not* nameable within the system. In a filesystem it's the inode of the root directory, hard-wired into the kernel. In DNS it's the **root hints file**: a small, statically shipped list of the root servers' addresses that every resolver is born knowing. That is the one piece of information resolution cannot bootstrap for itself.

DNS names read right-to-left as a path from the root: `www.example.com.` is `root → com → example → www`. The trailing dot is the root — usually elided, but it's the closure point made visible. Each edge label (`com`, `example`, `www`) is a lookup in a directory node, and the directory nodes are distributed across the internet on different administrative machines. That distribution is the whole reason resolution is interesting.

## Iterative vs. recursive: who does the walking

The textbook draws the sharpest distinction here, and it's about *who holds the work*.

In **iterative resolution**, the client asks a server for a name; if that server isn't authoritative for it, it doesn't chase the answer — it returns a *referral*: "I don't know, but here's the address of a server one level closer." The client then asks that next server, and repeats. The client does the walking; each server answers only for the edge it owns.

In **recursive resolution**, the client hands the whole name to one server and says "you go get it." That server queries the next on the client's behalf, which may in turn query the next, and the final answer bubbles back up the chain. The client does no walking; one server absorbs the latency and the work.

Real DNS is a hybrid, and knowing which half is which explains everything else:

| | Stub → recursive resolver | Recursive resolver → root/TLD/auth |
|---|---|---|
| Mode | **recursive** ("just get me the answer") | **iterative** (follows referrals itself) |
| Who walks | the resolver (e.g. `1.1.1.1`, `8.8.8.8`) | the resolver, one delegation at a time |
| Why | your laptop wants one round trip | root/TLD servers *refuse* to recurse — they only refer |

Root and TLD servers answering iteratively (referral only, never recursion) is what keeps them survivable: they do O(1) work per query and hold no per-client state. The recursive resolver is where the multi-step walk and all the caching actually live.

## Watching the walk with `dig +trace`

Your normal resolver hides all of this — it answers from cache in one hop. `dig +trace` deliberately turns caching off and reproduces the resolver's iterative walk from the root, printing each referral. Per the ISC docs, it "resolves the query from the root nameservers downwards and reports the results from each query step," making its own queries following the delegation referrals it receives rather than trusting a single upstream resolver. It's the closest thing to seeing the textbook's graph traversal on screen:

```
$ dig +trace www.example.com

; <<>> DiG 9.18 <<>> +trace www.example.com
.            518400  IN  NS  a.root-servers.net.      # (1) from root hints (closure):
.            518400  IN  NS  b.root-servers.net.      #     the 13 root servers, TTL 6 days
                     ...
com.         172800  IN  NS  a.gtld-servers.net.      # (2) root REFERS us down to .com:
com.         172800  IN  NS  b.gtld-servers.net.      #     "ask these for anything in com"
;; Received 1174 bytes from 198.41.0.4#53(a.root-servers.net)   # <- queried a root server

example.com. 172800  IN  NS  a.iana-servers.net.      # (3) .com REFERS us to example.com's
example.com. 172800  IN  NS  b.iana-servers.net.      #     authoritative servers
;; Received 771 bytes from 192.5.6.30#53(a.gtld-servers.net)    # <- queried a .com TLD server

www.example.com. 86400 IN A  23.215.0.136             # (4) AUTHORITATIVE answer, no referral:
;; Received 56 bytes from 199.43.135.53#53(a.iana-servers.net)  #     we reached the leaf node
```

Read it as a graph walk. Line (1) is the **closure point** — dig starts from the root hints, the one thing it didn't have to look up. Each `;; Received ... from` line names the directory node dig just queried. Lines (2) and (3) are **referrals** (`NS` records, no `A` for `www` yet): each server hands back the label one level down instead of the final answer — that's iterative mode, servers refusing to walk for you. Line (4) is different: an actual `A` record with no accompanying referral, and it came from `a.iana-servers.net`, the server **authoritative** for the `example.com` zone. No referral means you've hit the leaf. (The `example.com` A record is served from a CDN and rotates, so your exact IP may differ — the *structure* of the trace is what's stable.)

Two details worth staring at. The `NS` records at each step *are* the directory-node contents from the textbook model — the edge table telling you which node owns the next label. And when a referral points to a nameserver whose own address isn't obvious (e.g. `a.gtld-servers.net`), the parent zone ships **glue records** — the A/AAAA of the child's nameservers — inside the same response, so you don't need a separate lookup just to find the server you were told to ask. Without glue you'd have a chicken-and-egg loop; RFC 1034 bakes glue into delegations precisely to break it. (Where glue is missing, dig falls back to `/etc/resolv.conf` to resolve the nameserver's name — a small crack in the "pure trace" illusion.)

## Name-space implementation: zones, not nodes

The textbook stresses that a large name space isn't implemented node-by-node — it's *partitioned* into contiguous subgraphs, each managed by one authority, and split across three layers: a **global layer** (the root and TLDs, rarely changing, heavily replicated), an **administrational layer** (organizational domains like `example.com`), and a **managerial layer** (the fast-changing leaf records). DNS calls each managed partition a **zone**. A zone is the piece of the tree one authoritative server set actually owns; the boundary between a zone and its child zone is exactly a **delegation** — the `NS` referral you saw on lines (2) and (3). `com` is a zone; `example.com` is a *different* zone delegated from it. Every referral in the trace is a zone boundary being crossed.

This layering also dictates *availability strategy*, which the book frames as a design consequence rather than an accident. Global-layer nodes are read almost never-written and can be aggressively replicated and cached with long TTLs (root/TLD NS records above carry TTLs of days). Managerial-layer records change often and carry short TTLs. Which brings us to the last mechanism.

## Caching and TTL: making a global graph fast

A strict iterative walk from the root for *every* lookup would crush the root servers and add latency to everything. The fix is caching, and DNS puts the caching policy in the *data*: every record carries a **TTL** (time-to-live) set by the zone's owner, defined in RFC 1035 as the number of seconds the record may be cached before it must be re-fetched. Your recursive resolver caches each record — the `com` NS set, the `example.com` NS set, the final A — for its TTL. The next query for anything in `.com` skips the root entirely; the next query for `www.example.com` may skip everything and answer from cache in microseconds.

TTL is the single knob trading **freshness against load**. Long TTLs (the root's 518400s / 6 days) mean rarely-changing, heavily-cached, low-load. Short TTLs (60s on a record behind a load balancer) mean quick propagation of changes at the cost of more upstream queries. This is why `dig +trace` and a plain `dig www.example.com` disagree on latency and sometimes on the exact answer: the trace is a cold walk from closure with caching disabled, while your resolver is answering warm. Same graph, same closure point, same resolution algorithm — the only difference is how much of the walk was already remembered.

**Try next:** Run `dig +trace` against a domain you own or use daily, then run plain `dig` twice in a row and watch the `Query time` drop to near-zero on the second call — then compare the `TTL` column against how long that speedup lasts before the cache expires and the walk happens again.
