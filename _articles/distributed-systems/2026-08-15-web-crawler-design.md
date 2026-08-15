---
title: "Design a Web Crawler: The Frontier Is the Whole Problem"
date: 2026-08-15
track: distributed-systems
summary: "A crawler is a BFS loop wrapped around four hard problems: a URL frontier that balances priority against per-host politeness, dedup at both URL and content level (SimHash with 64-bit fingerprints, k=3), robots.txt compliance per RFC 9309, and not drowning in crawl traps. Mercator's two-tier frontier design, with pseudocode."
reading_time: 6
tags: [system-design, web-crawler, mercator, simhash, interview-prep]
sources:
  - title: "Heydon, A. & Najork, M. — Mercator: A Scalable, Extensible Web Crawler (1999)"
    url: "http://www.cs.ucr.edu/~vagelis/classes/CS242/publications/scalable-crawler.pdf"
  - title: "Olston, C. & Najork, M. — Web Crawling (Foundations and Trends in IR, 2010)"
    url: "http://i.stanford.edu/~olston/publications/crawling_survey.pdf"
  - title: "Manku, Jain & Das Sarma — Detecting Near-Duplicates for Web Crawling (WWW 2007)"
    url: "https://research.google.com/pubs/archive/33026.pdf"
  - title: "RFC 9309 — Robots Exclusion Protocol"
    url: "https://datatracker.ietf.org/doc/html/rfc9309"
---

A web crawler sounds like a queue and a fetch loop: pop URL, download, extract links, push links, repeat. That naive version melts in minutes — it DoSes the first big site it meets, re-downloads the same page through a thousand URL aliases, and wanders into an infinite calendar widget forever. The real design, laid out in Heydon and Najork's **Mercator** paper (1999) and still the reference architecture, is about four subsystems: the frontier, dedup, politeness, and trap defense.

## The URL frontier: priority in front, politeness in back

The frontier is "the data structure that contains all the URLs that remain to be downloaded" — and it cannot be one FIFO. Two goals fight each other: you want to fetch *important* URLs first (priority), but you must never hammer one host (politeness — Mercator guarantees at most one worker downloads from a given server at a time, keyed by canonical host name). The standard resolution, from Mercator's frontier as refined in Olston & Najork's survey, is two tiers:

- **Front queues** — one per priority class. A prioritizer (PageRank-ish score, in-degree, change rate, seed distance) assigns each URL to a class; higher classes are drained more often.
- **Back queues** — one per *host*, each strictly FIFO. A router moves URLs from front queues into the back queue for their host, and a min-heap orders hosts by the earliest time they may next be hit.

```text
frontier.enqueue(url):
    q = front_queue[ prioritize(url) ]           # by importance
    q.push(url)

frontier.next():
    (t_ok, host) = heap.pop_min()                # earliest permitted host
    wait_until(t_ok)
    url = back_queue[host].pop()
    if back_queue[host].empty():
        refill: pull from front queues (biased to high priority)
                until a URL for a *new* host arrives; create its
                back queue and heap entry
    else:
        heap.push( (now + politeness_delay(host), host) )
    return url

after fetch:  politeness_delay(host) = max(k * last_fetch_time, min_delay)
```

The delay rule matters: waiting a multiple *k* of the last download time (10x in the classic setup) automatically backs off on slow servers. Keep ~3x more back queues than worker threads so workers rarely starve. At web scale the frontier doesn't fit in RAM — Mercator kept queue heads/tails in memory and the middles on disk.

**BFS vs DFS:** BFS from good seeds, in practice. Breadth-first finds high-quality, high-in-degree pages early and spreads load across many hosts; DFS buries you deep in one site (a politeness nightmare) and one trap swallows the thread. Real crawlers are "BFS reordered by priority," which is exactly what the two-tier frontier implements.

## Politeness is a protocol: robots.txt

Before fetching from a new host, fetch `https://host/robots.txt` — now formally specified as **RFC 9309**, which defines the `User-agent` / `Allow` / `Disallow` group rules, longest-match-wins precedence, and a crucial operational detail: on server errors (5xx) for robots.txt you must assume *complete disallow*, while an unreachable/404 robots.txt means allow-all. Cache the parsed rules per host with a TTL (RFC 9309 says up to 24h is reasonable). While you're caching per-host state, cache **DNS** too: a resolver round-trip per URL can dominate fetch latency, and Mercator found stock synchronous resolvers so serializing that they built their own async one. A local caching resolver plus a `host → IP` map with TTLs is table stakes.

## Dedup: seen URLs and seen content are different problems

**URL-seen test.** Normalize first (lowercase host, strip default ports and fragments, resolve `..`, sort/strip tracking params) — then check membership in the set of ~billions of seen URLs. Options:

| Structure | Memory for 10B URLs | False positives | Notes |
|---|---|---|---|
| Exact hash set of URLs | ~1 TB | none | doesn't fit in RAM |
| 8-byte fingerprints, mem+disk (Mercator) | ~80 GB + disk | none | in-memory hash of popular URLs in front of a sorted disk file |
| Bloom filter | ~12 GB (10 bits/key) | tunable ~1% | a false positive silently *skips* a real URL — acceptable for crawling |

The Internet Archive's crawler used the Bloom-filter route; Mercator deliberately didn't, trading disk seeks for zero false negatives. The corpus [Bloom/cuckoo filter article](/articles/distributed-systems/2026-08-10-cuckoo-filters-vs-bloom) covers the mechanics.

**Content-seen test.** Mirrors and boilerplate mean different URLs serve near-identical pages. Exact checksums catch exact copies; for *near*-duplicates the production answer is **SimHash** (Manku, Jain & Das Sarma, WWW 2007, at Google): hash each page's features into a **64-bit fingerprint** such that similar pages differ in few bits, then flag pages within **Hamming distance k = 3** — parameters they validated on an 8B-page corpus, with precision/recall both near 0.75. Their trick for finding all fingerprints within distance 3 among billions: store several permuted, sorted copies of the fingerprint table so candidates share a long exact prefix.

## Traps, freshness, and the re-crawl loop

**Crawl traps** are URL spaces of unbounded size: calendars with a "next month" link forever, session IDs in URLs, faceted search combinatorics. Defenses are budgetary, not clever: cap URL length and path depth, cap pages-per-host per cycle, detect long repeating path segments (`/a/b/a/b/a/b/`), and let content dedup catch the fact that page 40,001 of the calendar looks like page 40,000.

Crawling is not one pass — pages change, so the frontier is really a **re-crawl scheduler**. Olston & Najork's survey frames it as maximizing freshness under a fetch budget: estimate each page's change rate (Poisson model fit from observed changes across visits), then set re-visit intervals accordingly. Two non-obvious results: crawling a page *more* often than it changes wastes budget, and for pages that change constantly it's optimal to nearly *give up* rather than burn budget staying hopelessly stale. Sitemaps and HTTP `If-Modified-Since`/`ETag` conditional GETs make revisits nearly free when nothing changed.

To distribute the whole thing, partition by host (hash of hostname → crawler node) so politeness state, robots cache, and DNS cache stay node-local; only the URL-seen test needs cross-node routing of discovered URLs to their owning partition.

**Try next:** build a single-machine polite crawler for one seed domain — asyncio fetchers, the two-tier frontier above with a per-host heap, `urllib.robotparser`, and a Bloom filter for URL-seen — then point it at a site with a calendar and watch which of your trap defenses actually fires.
