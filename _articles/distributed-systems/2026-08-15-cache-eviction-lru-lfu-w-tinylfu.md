---
title: "Cache Eviction at Scale: LRU, LFU, and Why Caffeine Ships W-TinyLFU"
date: 2026-08-15
track: distributed-systems
summary: "LRU forgets your hottest key the moment a scan floods the cache; LFU clings to yesterday's winners forever. W-TinyLFU fixes both by putting a Count-Min frequency sketch in front of an admission window and a segmented LRU. It's the default in Caffeine (3.2.4, May 2026) and Go's Ristretto (v2.4.2), and it's a favorite systems-interview question. Here's the policy and the admission decision it turns on."
reading_time: 6
tags: [caching, eviction, lru, lfu, w-tinylfu, count-min-sketch]
sources:
  - title: "TinyLFU: A Highly Efficient Cache Admission Policy (Einziger, Friedman, Manes)"
    url: "https://arxiv.org/abs/1512.00727"
  - title: "Caffeine Wiki — Efficiency (W-TinyLFU design)"
    url: "https://github.com/ben-manes/caffeine/wiki/Efficiency"
  - title: "Design of a Modern Cache—Part Deux (Ben Manes)"
    url: "https://highscalability.com/design-of-a-modern-cachepart-deux/"
  - title: "Caffeine — high performance caching library for Java"
    url: "https://github.com/ben-manes/caffeine"
  - title: "Ristretto — high performance memory-bound Go cache (v2)"
    url: "https://pkg.go.dev/github.com/dgraph-io/ristretto/v2"
---

An **LRU** cache holding your ten hottest keys will happily evict every one of them if a batch job scans a million cold rows through it once. An **LFU** cache won't — but it will also keep a key that was hammered a million times last Tuesday and never touched since, because its counter says "popular." Both classic policies optimize for exactly one signal: recency or frequency. Real workloads have both, and the interesting eviction policies are the ones that combine them. **W-TinyLFU** is the current answer, and it's what ships by default in Caffeine and Ristretto.

## The classics and how they break

**FIFO** evicts in insertion order — it ignores access entirely, so a hot key inserted early gets thrown out on schedule. **LRU** evicts the least-recently-used entry; it captures recency and temporal locality cheaply, but it has no memory of *how often* anything was used. Two failure modes follow: a **scan** (a burst of never-repeated keys) walks the entire cache out, and **one-hit wonders** — keys touched once — get admitted at the cost of a genuinely hot key.

**LFU** evicts the least-frequently-used entry, which resists scans beautifully. Its problem is **aging**: without a decay mechanism, counters only grow, so an item that was popular during a traffic spike stays resident long after its access rate collapses. Exact LFU also needs a counter per key and an O(log n) heap, which is expensive at scale.

| Workload | FIFO | LRU | LFU | W-TinyLFU |
|---|---|---|---|---|
| Recency-biased (temporal locality) | poor | strong | weak | strong |
| Frequency-biased (Zipfian popularity) | poor | weak | strong | strong |
| Large scan / sequential flood | poor | fails | resists | resists |
| One-hit wonders | poor | admits them | rejects them | rejects them |
| Shifting popularity over time | poor | adapts fast | ages badly | adapts (sketch resets) |

## W-TinyLFU: a sketch in front of an SLRU

W-TinyLFU (Window TinyLFU), introduced in the 2015 TinyLFU paper by Einziger, Friedman, and Manes, splits the cache into two parts and gates the boundary with a frequency filter.

- A small **admission window** — a plain LRU, ~1% of capacity — catches brand-new keys and recency bursts.
- A large **main region** — a **Segmented LRU (SLRU)** with a *probation* and a *protected* segment — holds everything that earned its place.
- A **TinyLFU admission filter** decides whether a key evicted from the window is good enough to displace a victim in the main region.

The filter's frequency estimate comes from a **Count-Min Sketch**: a compact 4-bit-per-counter table (Caffeine spends roughly 8 bytes per cache entry on it) that estimates how often a key has been seen without storing the keys themselves. Crucially, it **ages**: a running counter tracks total increments, and when it reaches a sample size proportional to cache capacity, *every counter is halved*. That periodic reset is what gives frequency information a fading memory — the exact thing plain LFU lacks — so a key that was hot last Tuesday decays out.

## The admission decision

When the window overflows, its LRU victim (the *candidate*) doesn't automatically enter the main region. TinyLFU compares the candidate's estimated frequency against the frequency of the main region's eviction *victim*. Higher frequency wins:

```text
onWindowEvict(candidate):
    victim        = mainRegion.evictionCandidate()   # SLRU probation tail
    candidateFreq = sketch.frequency(candidate)
    victimFreq    = sketch.frequency(victim)

    if candidateFreq > victimFreq:
        admit(candidate); evict(victim)              # candidate is genuinely hotter
    else if candidateFreq >= HASHDOS_THRESHOLD:
        # frequencies are close but non-trivial: admit with small probability
        # so an attacker can't pin cache contents by forging near-ties
        admit_with_probability(candidate, 1/128)
    else:
        reject(candidate)                            # keep the incumbent
```

This is why a scan can't wipe the cache: each scanned key has frequency ~1 and loses the comparison against any warm incumbent, so it never gets admitted past the window. The small randomized branch (Caffeine's HashDoS defense) prevents an adversary from freezing contents by manufacturing frequency ties.

Caffeine goes one step further with an **adaptive window**: it uses hill climbing to resize the window-vs-main split at runtime, sampling hit rate and stepping toward whatever ratio the current workload prefers — the window ramps up under recency-heavy load and shrinks back under frequency-heavy load.

## Where it runs, and the caveats

**Caffeine** (Java) is the reference implementation — Ben Manes' library, current release **3.2.4 (May 4, 2026)** — and it's the cache under Spring, Micronaut, and countless JVM services. Go's **Ristretto** (dgraph, **v2.4.2**, July 2026) uses a TinyLFU admission policy with the same sketch-plus-doorkeeper idea; the Rust **Moka** cache adopted the design too.

The trade-offs are honest ones. The Count-Min sketch is approximate, so frequency estimates can collide and mislead on adversarial keys. The sketch and SLRU add memory and moving parts that a 30-line LRU doesn't have, and for a tiny cache or a purely recency-bound workload plain LRU is simpler and just as good. W-TinyLFU earns its complexity when your cache is large, your key distribution is skewed, and scans or one-hit wonders would otherwise poison a naive policy.

**Try next:** run Caffeine's bundled simulator (`caffeine/simulator`) against a Zipfian trace and a sequential-scan trace, and compare `Lru` vs `WindowTinyLfu` hit ratios to watch the admission filter reject the scan.
