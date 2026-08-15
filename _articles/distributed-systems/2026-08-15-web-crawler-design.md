---
title: "Design a Web Crawler: The Frontier Is the Whole Problem"
date: 2026-08-15
track: distributed-systems
summary: "A crawler is a breadth-first loop wrapped around four hard problems: a URL frontier balancing priority against per-host politeness, deduplication at both URL and content level (SimHash with 64-bit fingerprints, k=3), robots.txt compliance per RFC 9309, and crawl-trap defence. Mercator's two-tier frontier, with a Scala sketch."
reading_time: 7
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

**Gist.** A naive crawler — pop a uniform resource locator (URL), download, extract links, push, repeat — overloads the first large site it meets, re-downloads one page through many URL aliases, and follows an unbounded calendar link space forever. The reference architecture, from Heydon and Najork's **Mercator** paper (1999) and Olston and Najork's later survey, replaces the single queue with a two-tier frontier that separates *which URL is worth fetching* from *when its host may next be contacted*, plus deduplication at the URL and content level. The cost is state: per-host queues, per-host politeness and robots caches, and a seen-URL set over billions of entries that no longer fits in memory.

## The URL frontier: priority in front, politeness in back

The frontier holds all URLs that remain to be downloaded, and it cannot be one first-in-first-out (FIFO) queue, because two goals conflict. Priority demands that important URLs be fetched first. Politeness demands that a single host never be hammered: **Mercator guarantees that at most one worker downloads from a given server at a time, keyed by canonical host name**. The resolution is two tiers.

- **Front queues** — one per priority class. A prioritizer (in-degree, estimated change rate, distance from seed, a PageRank-like score) assigns each URL to a class; higher classes are drained more often.
- **Back queues** — one per *host*, each strictly FIFO. A router moves URLs from front queues into the back queue owning their host. A **min-heap orders hosts by the earliest wall-clock time at which each may next be contacted.**

The invariant that makes politeness hold is structural rather than checked: **each host has exactly one back queue, and a host is absent from the heap while one of its URLs is in flight.** A worker cannot obtain two URLs of the same host concurrently, because obtaining one removes the host's heap entry, and only the completing worker reinserts it.

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

The delay rule is the second load-bearing detail: **waiting a multiple *k* of the host's last observed download time (ten times, in Mercator) backs off automatically on slow servers**, without measuring their capacity directly. Keeping roughly three times more back queues than worker threads reduces the frequency with which every non-empty queue belongs to a host still serving its delay. At web scale the frontier exceeds memory; Mercator held queue heads and tails in memory and the middles on disk.

**Breadth-first versus depth-first.** Breadth-first search from good seeds is the practical choice: it reaches high-in-degree pages early and spreads requests across many hosts, so the politeness heap rarely blocks. Depth-first concentrates requests on one host and lets a single trap consume a worker indefinitely. A production crawler is breadth-first reordered by priority, which is what the two-tier frontier implements.

## Politeness is a protocol: robots.txt

Before fetching from a new host, the crawler fetches `https://host/robots.txt`, specified in **RFC 9309**. The document defines `User-agent`, `Allow` and `Disallow` group rules with longest-match-wins precedence, and two asymmetric error behaviours that are easy to get backwards: **a 4xx response, robots.txt absent included, means allow-all, while a 5xx or an unreachable server means complete disallow.** Parsed rules are cached per host with a time-to-live (TTL); RFC 9309 says a crawler should not use a cached copy for more than 24 hours.

Domain Name System (DNS) state deserves the same per-host cache. A resolver round-trip per URL can dominate fetch latency, and Mercator reported that the platform's resolver interface serialized lookups badly enough that the authors implemented their own multi-threaded resolver. A local caching resolver plus a `host → IP` map honouring TTLs is the baseline.

## Deduplication: seen URLs and seen content are different problems

**URL-seen test.** Normalization comes first — lowercase the host, strip default ports and fragments, resolve `..` segments, sort or strip tracking parameters — then membership is tested against a set of billions of seen URLs.

| Structure | Memory for 10B URLs | False positives | Notes |
|---|---|---|---|
| Exact hash set of URLs | ~1 TB | none | does not fit in RAM |
| 8-byte fingerprints, mem+disk (Mercator) | ~80 GB + disk | none | in-memory hash of popular URLs in front of a sorted disk file |
| Bloom filter | ~12 GB (10 bits/key) | tunable ~1% | a false positive silently *skips* a real URL — acceptable for crawling |

The asymmetry decides the choice. **A Bloom-filter false positive suppresses a URL that was never crawled**, an error a crawler tolerates because coverage is already incomplete; Mercator instead paid disk seeks for an exact test. The corpus [Bloom/cuckoo filter article](/articles/distributed-systems/2026-08-10-cuckoo-filters-vs-bloom) covers the mechanics.

**Content-seen test.** Mirrors and shared boilerplate serve near-identical pages under distinct URLs. Exact checksums catch exact copies only. For near-duplicates the published production answer is **SimHash** (Manku, Jain and Das Sarma, WWW 2007, at Google): each page's features are hashed into a **64-bit fingerprint** such that similar pages differ in few bits, and pages within **Hamming distance k = 3** are flagged as duplicates — the paper reports k = 3 as the setting that balanced precision against recall in its manual evaluation, over a corpus of 8 billion pages. To find all fingerprints within distance 3 among billions, they store several permuted, sorted copies of the fingerprint table, so that candidate pairs share a long exact prefix in at least one permutation and can be found by binary search.

### Implementation sketch (Scala)

The politeness core of the frontier: the heap holds one entry per host, and reinsertion after a fetch is the only path back in.

```scala
import scala.collection.mutable

final case class Ready(atMillis: Long, host: String)

final class Frontier(minDelayMs: Long, k: Int):
  private val backQueues = mutable.Map.empty[String, mutable.Queue[String]]
  private val inFlight = mutable.Set.empty[String]
  private val heap = mutable.PriorityQueue.empty[Ready](
    Ordering.by[Ready, Long](r => -r.atMillis)             // negated: max-heap becomes min-heap
  )

  def enqueue(host: String, url: String): Unit = synchronized:
    val q = backQueues.getOrElseUpdate(host, mutable.Queue.empty)
    val fresh = q.isEmpty && !inFlight.contains(host)
    q.enqueue(url)
    if fresh then heap.enqueue(Ready(System.currentTimeMillis(), host))

  /** Removes the host from the heap, so no second worker can take the same host. */
  def next(): Option[(String, String)] = synchronized:
    // `head`, not `headOption`: only `head` is overridden to respect the ordering
    if heap.isEmpty || heap.head.atMillis > System.currentTimeMillis() then None
    else
      val r = heap.dequeue()
      inFlight += r.host
      Some((r.host, backQueues(r.host).dequeue()))

  /** Only completion reinserts, which is what enforces one fetch per host at a time. */
  def completed(host: String, fetchMillis: Long): Unit = synchronized:
    inFlight -= host
    if backQueues(host).nonEmpty then
      val delay = math.max(k * fetchMillis, minDelayMs)
      heap.enqueue(Ready(System.currentTimeMillis() + delay, host))
```

## Traps, freshness, and the re-crawl loop

**Crawl traps** are URL spaces of unbounded size: calendars with a perpetual "next month" link, session identifiers embedded in paths, faceted-search combinatorics. The defences are budgetary: cap URL length and path depth, cap pages fetched per host per cycle, detect repeating path segments (`/a/b/a/b/a/b/`), and rely on content deduplication to observe that calendar page 40,001 resembles page 40,000.

Crawling is not a single pass. Pages change, so the frontier is a **re-crawl scheduler**, framed in Olston and Najork's survey as maximizing freshness under a fixed fetch budget: estimate each page's change rate with a Poisson model fitted to changes observed across visits, then set revisit intervals from it. Two results run against intuition: **crawling a page more often than it changes spends budget for no freshness gain**, and for pages that change constantly it is preferable to nearly abandon them rather than spend budget remaining hopelessly stale. Sitemaps and conditional GET requests using `If-Modified-Since` or `ETag` make an unchanged revisit close to free.

Distribution partitions by host — hash of hostname to crawler node — so that politeness state, robots cache and DNS cache remain node-local. **Only the URL-seen test requires cross-node traffic**, routing each discovered URL to the node owning its host.

## Pitfalls

- Treating a 5xx response for robots.txt as "no rules" and crawling the host: RFC 9309 mandates complete disallow for that case, and the symptom is a crawl that accelerates precisely when a site is already failing.
- Sharing one back queue across hosts: two workers then fetch the same host concurrently, breaking Mercator's one-connection-per-server guarantee even though every individual delay was honoured.
- Reinserting a host into the heap at dequeue time rather than at fetch completion: the delay is then measured from request start, so slow hosts receive requests faster than fast ones.
- Fewer back queues than workers: workers idle while the only non-empty queues belong to hosts still serving their politeness delay, and throughput collapses without any host being saturated.
- Skipping URL normalization: fragments, default ports and tracking parameters produce distinct keys for one page, so the seen-URL set grows without bound and the same content is fetched repeatedly.
- Relying on exact checksums for the content-seen test: mirrors differing by one advertisement or timestamp hash differently, and the corpus fills with near-duplicates that SimHash at Hamming distance 3 would have collapsed.
- Depth-first traversal: a single calendar trap consumes a worker indefinitely, and because the deep path stays on one host, the crawler both stalls and behaves impolitely.
