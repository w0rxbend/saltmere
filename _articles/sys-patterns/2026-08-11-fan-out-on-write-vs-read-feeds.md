---
title: "Fan-out on write vs read: how a timeline actually gets built"
date: 2026-08-11
track: sys-patterns
summary: "The classic news-feed question is a delivery trade-off. Push a copy of every post into each follower's precomputed timeline (fast reads, brutal writes) or store posts once and assemble at read time (cheap writes, slow reads). Real systems run both at once and merge — here's the worker, the Redis timeline, and the celebrity escape hatch."
reading_time: 6
tags: [fan-out, news-feed, redis, timeline-cache, system-design]
sources:
  - title: "System Design Primer — Design the Twitter timeline and search"
    url: "https://github.com/donnemartin/system-design-primer/tree/master/solutions/system_design/twitter"
  - title: "High Scalability — The Architecture Twitter Uses to Deal with 150M Active Users"
    url: "https://highscalability.com/the-architecture-twitter-uses-to-deal-with-150m-active-users/"
  - title: "Raffi Krikorian — Twitter Timelines at Scale"
    url: "https://speakerdeck.com/angelbotto/raffi-krikorian-twitter-timelines-at-scale"
  - title: "Redis — Sorted sets data type"
    url: "https://redis.io/docs/latest/develop/data-types/sorted-sets/"
---

Every "design a news feed" interview is really one question: **when do you pay for assembling a user's timeline — at write time or at read time?** A timeline is just the union of recent posts from everyone you follow, ordered newest-first. That query is trivial to describe and miserable to serve, because reads dwarf writes (people scroll far more than they post) and the fan degree is wildly skewed (most accounts have dozens of followers; a handful have tens of millions). The two pure strategies sit at opposite ends of that trade-off, and the systems you actually use run a blend of both.

## Fan-out on write (push)

When a user posts, immediately write a copy of the post ID into the **precomputed home timeline** of every one of their followers. Timelines are materialized ahead of time, so a read is one cheap lookup: fetch the list, hydrate the post IDs, done. This is how Twitter served the home timeline — a per-user Redis list, fanned out by a worker pool, with a read hitting an already-assembled list of tweet IDs.

The cost is on the write side and it is nonlinear. A post by someone with N followers triggers N inserts. For a normal user that's nothing. For an account with 30 million followers it's 30 million writes for a single tweet — a **thundering-herd write** that saturates the fan-out queue, delays delivery (some fan-outs took minutes), and blows up storage: the same post ID is now duplicated into millions of lists. You also fan out to inactive followers who will never read it, and to accounts that muted or unfollowed a second later.

## Fan-out on read (pull)

The inverse: store each post exactly once, keyed by author. Nobody's timeline is materialized. At read time, look up who the user follows, query each followee's recent posts, merge them k-way by timestamp, and return the top slice. Writes are now trivial — one insert per post regardless of follower count, so celebrities cost the same as anyone else. Storage is minimal; there's no duplication.

The cost moves to reads, which are the hot path. A user following 500 accounts triggers a 500-way scatter-gather on every timeline load and every scroll, and reads outnumber writes by orders of magnitude. Caching helps, but a pure pull model makes your most frequent operation your most expensive one — exactly backwards.

## The hybrid everyone actually ships

Neither pure strategy survives contact with a real follower distribution, so production systems (Twitter, Instagram) split by fan degree:

- **Push for the long tail.** Ordinary authors fan out on write into follower timelines. Cheap writes, instant reads.
- **Pull for celebrities.** Accounts above a follower threshold are *excluded* from fan-out. Their posts are fetched at read time and **merged** into the requesting user's timeline, then re-ranked before serving.

So a home-timeline read becomes: take the precomputed list (everything pushed by the accounts you follow) and merge in the freshly-pulled recent posts of the few celebrities you follow. You pay the small pull cost only for the handful of high-degree accounts, and you never fan a 30-million-write storm into the queue.

## The moving parts

**Fan-out queue.** Posting writes the post to durable storage and enqueues a `post.created` event. A worker pool drains it asynchronously, so the author's write returns immediately and delivery happens off the critical path.

**Timeline cache.** Each user's home timeline is a bounded, ordered structure in Redis. A **sorted set** is the right primitive: score = post timestamp (or a Snowflake ID that encodes time), member = post ID. `ZADD` inserts in order, `ZREVRANGE`/`ZREVRANGEBYSCORE` reads newest-first, and `ZREMRANGEBYRANK` trims the list to the last few hundred entries so memory stays bounded.

```python
# Worker consuming a post.created event
def handle_post_created(evt):
    author, post_id, ts = evt.author_id, evt.post_id, evt.created_at
    if follower_count(author) >= CELEBRITY_THRESHOLD:
        return  # skip fan-out; celebrities are pulled at read time

    for follower_id in iter_followers(author):          # batched, paginated
        key = f"timeline:{follower_id}"
        pipe.zadd(key, {post_id: ts})
        pipe.zremrangebyrank(key, 0, -(MAX_TIMELINE + 1))  # keep newest MAX
    pipe.execute()

# Read path: merge pushed timeline with pulled celebrity posts
def home_timeline(user_id, cursor):
    pushed = redis.zrevrangebyscore(
        f"timeline:{user_id}", max=cursor, min="-inf",
        start=0, num=PAGE, withscores=True)

    celebs = celebrity_followees(user_id)               # small set
    pulled = [p for c in celebs
                for p in recent_posts(c, before=cursor)]

    merged = heapq.merge(pushed, pulled, key=score, reverse=True)
    return rank(dedupe(merged))[:PAGE]
```

`cursor` here is a `(timestamp, post_id)` pair, not a page number — the merged stream shifts constantly as new posts land, so `OFFSET`-style paging would skip and duplicate rows. Page it with **keyset (seek) pagination**, carrying the last item's sort key forward; see [keyset pagination vs OFFSET](/articles/microservices/2026-08-10-pagination-offset-vs-keyset) for the cursor mechanics and why the composite key matters.

## Trade-offs

| Dimension | Fan-out on write (push) | Fan-out on read (pull) | Hybrid |
|---|---|---|---|
| Write cost | O(followers) per post | O(1) per post | O(1) for celebrities, O(followers) otherwise |
| Read cost | O(1) lookup | O(followees) scatter-gather | Cheap + small pull |
| Storage | High (duplicated per follower) | Low (single copy) | Moderate |
| Celebrity post | Thundering-herd write, minutes of lag | Trivial | Pulled at read time |
| Freshness | Delayed by queue depth | Always current | Mixed; celebrities current |
| Failure mode | Queue backlog, timeline drift | Slow reads under load | Complexity of two paths |

The engineering judgment is the threshold. Set it too low and you pull too much at read time; too high and one viral account can still stall the queue. Most designs pick a follower count, treat it as a tunable, and reconcile edge cases (a normal user who suddenly goes viral) by promoting them to pull and lazily backfilling.

**Try next:** instrument fan-out lag — measure post-to-timeline delivery time at p50/p99, then sweep the celebrity threshold and watch write-queue depth and read-merge latency trade against each other.
