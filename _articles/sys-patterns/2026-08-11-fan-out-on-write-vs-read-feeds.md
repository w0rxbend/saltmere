---
title: "Fan-out on write vs read: how a timeline gets built"
date: 2026-08-11
track: sys-patterns
summary: "The news-feed question is a delivery trade-off: push a copy of every post into each follower's precomputed timeline (cheap reads, expensive writes), or store each post once and assemble at read time (cheap writes, expensive reads). Production systems run both and merge — the fan-out worker, the Redis timeline, and the celebrity exclusion."
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

**Gist.** A home timeline is the union of recent posts from every followed account, ordered newest-first — a query that is easy to state and expensive to serve, because reads greatly outnumber writes and the follower distribution is heavily skewed. Two mechanisms resolve it: **fan-out on write**, which materializes a per-follower timeline at post time, and **fan-out on read**, which stores each post once and merges at query time. The first buys a single ordered range scan per read at the cost of O(followers) writes and duplicated storage; the second buys O(1) writes with an O(followees) scatter-gather on the hottest path.

## Fan-out on write (push)

On publication, a copy of the post identifier is written into the **precomputed home timeline** of every follower. Timelines are materialized ahead of the read, so serving a timeline is one ordered range scan followed by hydration of the identifiers into post bodies. Twitter served the home timeline this way: a per-user Redis structure of tweet identifiers, populated by a worker pool, read as an already-assembled list.

The write cost scales with fan degree. A post by an author with N followers triggers **N inserts for one logical write**. For an account with tens of millions of followers this is a burst large enough to saturate the fan-out queue; Krikorian's account of Twitter timelines identifies these high-degree accounts as the ones whose deliveries lag, so that the last follower receives a post measurably later than the first. Storage inflates in the same proportion, because the identifier is duplicated into every follower's list. The burst is also partly wasted: it reaches inactive followers who never read the timeline, and followers who mute or unfollow immediately afterwards.

## Fan-out on read (pull)

The inverse stores each post exactly once, keyed by author, and materializes no timeline. A read resolves the follow set, queries each followee's recent posts, merges those streams by sort key, and returns the top slice. Writes become **one insert per post independent of follower count**, so a high-degree account costs no more than any other, and storage carries a single copy.

The cost relocates to the read path, which is the frequent one. A user following 500 accounts performs a 500-way scatter-gather on every timeline load and every subsequent page. Caching reduces the constant but not the shape: the pure pull model makes the most frequent operation the most expensive.

## The hybrid

Production systems partition authors by fan degree and run both mechanisms:

- **Push for the long tail.** Ordinary authors fan out on write into follower timelines.
- **Pull for high-degree accounts.** Authors above a follower threshold are **excluded from fan-out**; their recent posts are fetched at read time and merged into the requester's timeline at serving time.

A timeline read is therefore a merge of two streams: the precomputed list holding everything pushed by ordinary followees, and the freshly pulled recent posts of the small set of high-degree followees. The scatter-gather is bounded by the number of high-degree accounts a user follows rather than by the whole follow set, and no single post enqueues a multi-million-insert burst.

## Moving parts

**Fan-out queue.** Publication writes the post to durable storage and enqueues a `post.created` event. A worker pool drains the queue asynchronously, so the author's write returns without waiting for delivery. The queue depth is the observable signal of fan-out lag: **timeline delivery is eventually consistent, and its staleness equals the drain time of the backlog**.

**Timeline cache.** Each home timeline is a bounded ordered structure in Redis. A **sorted set** matches the access pattern: score = post timestamp, or a Snowflake identifier whose high bits encode time; member = post identifier. `ZADD` inserts in score order, `ZREVRANGE` and `ZREVRANGEBYSCORE` read newest-first, and `ZREMRANGEBYRANK` trims to a fixed number of entries so per-user memory is bounded. Trimming is what keeps the duplication cost of push finite: the structure holds a window, not a history.

**Cursor.** The cursor is a `(timestamp, post_id)` pair rather than a page number. The merged stream shifts as new posts land, so `OFFSET`-style paging skips and repeats rows. Paging uses **keyset (seek) pagination**, carrying the last item's composite sort key forward; see [keyset pagination vs OFFSET](/articles/microservices/2026-08-10-pagination-offset-vs-keyset) for the cursor mechanics.

### Implementation sketch (Scala)

```scala
final case class Post(id: Long, authorId: Long, score: Long)  // score: time-ordered id

// Write path: skip fan-out above the threshold, trim on every insert.
def onPostCreated(p: Post): Unit =
  if followerCount(p.authorId) >= CelebrityThreshold then ()   // pulled at read time
  else
    followers(p.authorId).grouped(1000).foreach: batch =>
      val pipe = redis.pipelined()
      batch.foreach: f =>
        val key = s"timeline:$f"
        pipe.zadd(key, p.score.toDouble, p.id.toString)
        pipe.zremrangeByRank(key, 0, -(MaxTimeline + 1))       // keep newest MaxTimeline
      pipe.sync()

// Read path: merge the materialized window with the pulled high-degree posts.
def homeTimeline(userId: Long, cursor: Long, page: Int): Vector[Post] =
  val pushed: Vector[Post] =
    redis.zrevrangeByScore(s"timeline:$userId", (cursor - 1).toDouble, Double.NegativeInfinity, 0, page)
      .asScala.toVector.map(hydrate)

  val pulled: Vector[Post] =
    celebrityFollowees(userId).flatMap(recentPosts(_, before = cursor))

  (pushed ++ pulled)
    .distinctBy(_.id)                 // a promoted author can appear on both paths
    .sortBy(-_.score)
    .take(page)
```

The `distinctBy` is load-bearing rather than defensive: an author crossing the threshold in either direction can be present in the materialized window **and** in the pull set for the same post.

## Trade-offs

| Dimension | Fan-out on write (push) | Fan-out on read (pull) | Hybrid |
|---|---|---|---|
| Write cost | O(followers) per post | O(1) per post | O(1) above threshold, O(followers) below |
| Read cost | One range scan | O(followees) scatter-gather | Range scan plus bounded pull |
| Storage | High (one copy per follower) | Low (single copy) | Moderate |
| High-degree post | Large write burst, delivery lag | Unchanged cost | Pulled at read time |
| Freshness | Delayed by queue depth | Current | Mixed; pulled authors current |
| Failure mode | Queue backlog, timeline drift | Read latency under load | Two code paths to keep consistent |

The tunable parameter is the threshold. Set low, the read path pulls from too many authors; set high, a single account with a large following can still stall the queue. The transition case — an account whose following grows past the threshold — has to be handled explicitly, because a post can be half delivered by push before the author is reclassified onto the pull path.

## Pitfalls

- **`OFFSET` paging on a merged stream.** Symptom: items appear twice or vanish between pages. Cause: new posts shift every subsequent row's offset while the reader pages; the position is only stable under a composite keyset cursor.
- **Untrimmed timelines.** Symptom: Redis memory grows without bound and eviction starts discarding whole timelines. Cause: `ZADD` without a paired `ZREMRANGEBYRANK`, so each timeline retains full history rather than a window.
- **Fan-out on delete.** Symptom: a deleted post remains visible in some timelines. Cause: the identifier was copied into every follower's sorted set, so deletion must repeat the fan-out or be filtered at hydration time.
- **Threshold transitions.** Symptom: duplicate entries for one post, or a gap around the promotion moment. Cause: the post was pushed to part of the follower set before the author was reclassified, and is then also returned by the pull path.
- **Backfilling a new follow.** Symptom: a newly followed ordinary account contributes nothing until its next post. Cause: fan-out on write populates timelines forward only; existing posts were copied before the follow edge existed.
- **Fan-out to inactive followers.** Symptom: queue depth and memory scale with registered accounts rather than with readers. Cause: the worker iterates the whole follower set, including accounts that never load a timeline.
