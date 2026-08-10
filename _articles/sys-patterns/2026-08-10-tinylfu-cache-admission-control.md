---
title: "Cache Admission Control with TinyLFU: Deciding What to Let In"
date: 2026-08-10
track: sys-patterns
summary: "Eviction alone answers only half the question. LRU admits every new item unconditionally, throwing out something that may be more valuable. TinyLFU adds an admission filter: a Count-Min frequency sketch with 4-bit counters, a doorkeeper bloom filter for one-hit-wonders, and an aging reset that halves all counters to track recent popularity. We cover the admit() decision, the aging step, and why Window-TinyLFU (Caffeine) bolts a small LRU window in front to survive bursts and scans."
reading_time: 6
tags:
  - caching
  - admission-control
  - tinylfu
  - w-tinylfu
  - count-min-sketch
  - bloom-filter
  - interview-prep
sources:
  - title: "Einziger, Friedman & Manes — TinyLFU: A Highly Efficient Cache Admission Policy (arXiv:1512.00727)"
    url: "https://arxiv.org/abs/1512.00727"
  - title: "Einziger, Friedman & Manes — TinyLFU (ACM Transactions on Storage, Vol. 13 No. 4, 2017)"
    url: "https://dl.acm.org/doi/10.1145/3149371"
  - title: "Caffeine Wiki — Design (Window TinyLFU, FrequencySketch)"
    url: "https://github.com/ben-manes/caffeine/wiki/Design"
  - title: "Ben Manes — Design of a Modern Cache (High Scalability)"
    url: "https://highscalability.com/design-of-a-modern-cache/"
  - title: "Caffeine Wiki — Efficiency (hit-rate simulations vs LRU/ARC/LIRS)"
    url: "https://github.com/ben-manes/caffeine/wiki/Efficiency"
---

Most caching discussions stop at eviction: given a full cache, which resident do we throw out? That is only half the decision. There is a second, quieter question — *should the incoming item be let in at all?* Classic LRU never asks it. On every miss it admits the new key unconditionally and evicts the least-recently-used resident to make room. That is a fine reflex until the newcomer is garbage: a scan touching a million cold keys once each will march straight through an LRU cache, evicting genuinely hot data to cache items that will never be requested again. The eviction policy did its job; the *admission* policy — which LRU lacks — was the missing guard.

**Admission control** flips the burden of proof onto the newcomer. When the cache is full, before we admit a candidate we compare its estimated long-term frequency against the frequency of the eviction victim the resident policy has already nominated. Admit only if the candidate looks *more useful* than what we would sacrifice. TinyLFU is the canonical, memory-cheap way to make that comparison.

## The core idea: admit() compares frequencies

The eviction policy (LRU, SLRU, whatever) still chooses a victim. TinyLFU sits in front as a filter:

```
function admit(candidate):
    victim = evictionPolicy.chooseVictim()
    if estimate(candidate) > estimate(victim):
        return ADMIT      # newcomer is worth more; evict victim
    else:
        return REJECT     # keep victim; do not cache candidate
```

Notice what this buys you: a one-hit scan key has `estimate(candidate) == 1` (or 0), while a hot resident has a high estimate. The comparison fails, the scan key is rejected, and the hot resident survives. Frequency-based *admission* gives you scan resistance for free, without paying LFU's usual costs of unbounded counters and poor recency handling.

The whole design hinges on `estimate()` being both accurate and tiny. A precise per-key frequency table would cost more memory than the cache it protects. TinyLFU instead approximates.

## The frequency sketch: Count-Min with 4-bit counters

TinyLFU's `estimate()` is backed by a **Count-Min sketch** (see the [Count-Min sketch article](/articles/distributed-systems/2026-08-10-count-min-sketch) for the mechanics — a matrix of counters, `d` hash functions, per-key minimum-over-rows read). We build on it here rather than re-derive it.

Two adaptations matter for caching:

- **4-bit counters.** We are not counting exact access totals; we only need to compare which of two items is *more* popular. A counter that saturates at 15 is plenty — Caffeine's `FrequencySketch` uses a 4-bit Count-Min sketch, packing counters so that the whole sketch costs roughly 8 bytes per cache entry. That is the difference between a sketch that fits in cache-friendly memory and one that does not.
- **Minimal increment.** On access, TinyLFU reads all `d` counters for the key and increments *only the smallest one(s)*. If the counters read `{2, 2, 5}`, only the two 2s become 3. This suppresses the Count-Min sketch's systematic over-count and keeps rare-key estimates honest.

`estimate(key)` returns the minimum across the key's `d` counters, exactly as in Count-Min.

## The doorkeeper: a bloom filter for one-hit-wonders

Skewed workloads have a long tail: most distinct keys are seen *once* inside any measurement window. Allocating full multi-bit sketch counters to keys that never recur is waste. TinyLFU's fix, from section 3.4 of the paper, is a **doorkeeper** — a plain bloom filter placed *in front of* the sketch.

> "The Doorkeeper is a regular Bloom filter placed in front of the approximate counting scheme. Upon item arrival, we first check if the item is contained in the Doorkeeper. If it is not contained in the Doorkeeper (as is expected with first timers and tail items), the item is inserted to the Doorkeeper and otherwise, it is inserted to the main structure."

So a key's *first* sighting only flips bits in the doorkeeper; it never touches the sketch. Its *second* sighting promotes it to the main sketch. Estimation combines both: if the key is present in the doorkeeper, TinyLFU adds 1 to whatever the main sketch reports.

```
function estimate(key):
    e = sketch.estimate(key)          # min over d counters
    if doorkeeper.contains(key):
        e = e + 1
    return e

function record(key):                 # called on every access
    if doorkeeper.contains(key):
        sketch.increment(key)         # seen before -> real counter
    else:
        doorkeeper.add(key)           # first-timer -> 1 bit only
    onIncrement()                     # drive the aging clock
```

The payoff, in the authors' words: "most tail items are only allocated 1 bit counters (in the Doorkeeper)... in many skewed workloads, this optimization significantly reduces the memory consumption of TinyLFU." One-hit-wonders cost a single bit and, with an estimate of 1, lose the `admit()` comparison to any genuinely warm resident.

## Aging: halve everything so recency wins

An LFU that never forgets is a museum: yesterday's viral key keeps its high count forever and blocks today's rising star. TinyLFU keeps its picture *fresh* with a reset (aging) step. A running counter tracks total increments; when it reaches the **sample size `W`**, every counter in the sketch is divided by two, and the doorkeeper is cleared.

```
sampleSize = W               # e.g. ~ 10x cache capacity
count = 0

function onIncrement():
    count += 1
    if count >= sampleSize:
        reset()

function reset():
    for c in sketch.counters:
        c = c >> 1           # halve (integer divide by 2)
    doorkeeper.clear()       # tail estimates start fresh
    count = count >> 1       # or recompute from remaining mass
```

Halving is a decay with a half-life of one window: a key must keep earning hits to stay estimated-popular, but recent popularity is weighted far above ancient popularity. Because the divide preserves *ratios*, the relative ordering that `admit()` cares about is retained while absolute magnitudes shrink — and 4-bit counters never overflow, since they are periodically pulled back down. This is what makes TinyLFU behave like a *windowed* LFU rather than a true all-time LFU.

## Window-TinyLFU: surviving bursts and warm-up

Pure TinyLFU admission has a weakness: an item arriving in a *sparse burst* — hit several times in quick succession, then again much later — may not have built up sketch frequency yet, so `admit()` rejects it before it can prove itself. Caffeine's **Window-TinyLFU (W-TinyLFU)** fixes this with a hybrid layout:

- A small **admission window**, managed as an LRU (historically ~1% of capacity). Every new item enters here first, no questions asked.
- A large **main region** (~99%), managed as **SLRU** (segmented LRU: a probation segment plus a protected segment capped around 80% of the main space — see [eviction policies](/articles/sys-patterns/2026-08-10-cache-eviction-policies)).

When the window evicts its LRU victim, that victim is *not* discarded — it becomes the *candidate* for the main region, and only then does the TinyLFU filter run: `estimate(windowVictim) > estimate(mainVictim)` decides admission into the main region. The window absorbs recency and bursts; the frequency filter guards the durable working set against scans. Caffeine further **adapts** the window/main split at runtime via hill climbing on the observed hit rate — larger windows for recency-heavy workloads, smaller for frequency-biased ones. See the [Caffeine article](/articles/scala-jvm/2026-08-10-caffeine-w-tinylfu-caching) for the JVM-side API.

## Why it raises hit ratio

On uniform-random access, admission control does nothing useful — every key is equally worthless, and the overhead is pure cost. Its value shows on the workloads real caches actually see:

- **Skewed (Zipfian) popularity.** The sketch cheaply distinguishes the head from the tail; `admit()` protects the head, and the doorkeeper keeps the tail from wasting counters. Caffeine's simulations show W-TinyLFU tracking near-optimal hit rates where LRU trails badly.
- **Scan / loop workloads.** A one-pass scan of cold keys can never win the frequency comparison against a warm resident, so it is rejected at the door instead of evicting the working set. LRU, lacking admission, is defenceless here.

The mental model worth keeping for interviews: **eviction picks who leaves; admission decides whether the newcomer has earned the seat.** LRU only does the former and treats every arrival as automatically deserving. TinyLFU makes admission a frequency argument, and does it in about a byte per entry.

**Try next:** implement the 4-bit Count-Min `FrequencySketch` with minimal increment and the halving reset, feed it a Zipf(1.0) key stream interleaved with a periodic full scan, and plot hit ratio for (a) plain LRU vs (b) LRU + TinyLFU admission vs (c) W-TinyLFU — then watch the scan pass leave the TinyLFU variants' working sets untouched.
