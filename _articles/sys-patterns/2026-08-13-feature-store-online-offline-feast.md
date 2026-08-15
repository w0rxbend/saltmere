---
title: "Feature stores: eliminating train/serve skew with online and offline stores"
date: 2026-08-13
track: sys-patterns
summary: "A feature store is one feature definition serving two stores: an offline store for point-in-time-correct training data and an online store for low-latency serving. That symmetry is what removes train/serve skew. The pattern is described here with Feast as the concrete example."
reading_time: 6
tags: [ml-infrastructure, feature-store, feast, online-store, point-in-time-join, ai-infrastructure, sys-patterns]
sources:
  - title: "Feast: the Open Source Feature Store — Introduction (docs)"
    url: "https://docs.feast.dev/"
  - title: "Point-in-time joins — Feast documentation"
    url: "https://docs.feast.dev/getting-started/concepts/point-in-time-joins"
  - title: "Feature retrieval — Feast documentation"
    url: "https://docs.feast.dev/getting-started/concepts/feature-retrieval"
  - title: "Releases — feast-dev/feast (GitHub)"
    url: "https://github.com/feast-dev/feast/releases"
  - title: "Meet Michelangelo: Uber's Machine Learning Platform"
    url: "https://www.uber.com/blog/michelangelo-machine-learning-platform/"
---

**Gist.** When a feature is computed one way in the training pipeline and a subtly different way in the serving path, the model scores well offline and degrades in production without raising an error; this divergence is **train/serve skew**. A feature store answers it with a single feature definition materialized into two stores — an offline store holding the full timestamped history for training, an online store holding the latest value per entity for inference — so both paths derive from one computation. The cost is a materialization pipeline that must be operated: the online store is a lagging copy, and its lag becomes a per-feature freshness parameter with its own failure modes.

Uber's Michelangelo describes this offline/online split as part of its platform architecture. Feast is an open-source implementation of the pattern; its release history is on GitHub, and the API shown below is the one its documentation describes.

## Two stores, one definition

- **Offline store** (warehouse or columnar files: BigQuery, Snowflake, parquet): the full history of every feature value with its event timestamp. It feeds training and batch scoring, and is optimized for large point-in-time joins rather than for latency.
- **Online store** (low-latency key-value store: Redis, DynamoDB, PostgreSQL): only the *latest* value per entity. It answers single-key reads on the request path, where the latency budget is the model's own inference budget; no published benchmark is cited here for a specific figure.

Both are described by the same `FeatureView`. The transformation is written once; Feast lands the result in each store. The **entity** supplies the join key — the identifier under which values are keyed online and matched offline. Skew is removed because training and serving read values that descend from one source of truth rather than from two independently maintained implementations.

```python
from feast import Entity, FeatureView, Field, FileSource
from feast.types import Float32, Int64
from datetime import timedelta

driver = Entity(name="driver", join_keys=["driver_id"])

driver_stats = FeatureView(
    name="driver_hourly_stats",
    entities=[driver],
    ttl=timedelta(days=3),
    schema=[
        Field(name="conv_rate", dtype=Float32),
        Field(name="trips_today", dtype=Int64),
    ],
    source=FileSource(path="driver_stats.parquet",
                      timestamp_field="event_timestamp"),
)
```

## Point-in-time correctness

A training set cannot be assembled by a naive equality join on `driver_id`, because such a join admits feature rows from the future of the labeled event. If an event is labeled at 10:00 and a feature value computed at 10:05 is attached to it, the model is trained on information that will not exist at inference time. The offline metric that results is not an estimate of production behaviour; it is an estimate of behaviour under an oracle.

The corrective mechanism is the **point-in-time (as-of) join**. The caller supplies an *entity dataframe*: one row per labeled event, carrying the join key and the event timestamp. For each such row the store selects, per feature view, the feature row that satisfies two conditions simultaneously:

1. its event timestamp is **at or before** the entity row's timestamp, and
2. it is the **newest** such row, subject to the feature view's `ttl` — a feature row older than `ttl` relative to the event does not qualify, and the feature is returned as null.

The `ttl` therefore does double duty. Offline it bounds how far back the as-of scan may reach, which keeps a stale value from being silently presented as current. Online it governs how long a materialized value remains valid. The two uses are consequences of one declaration, which is what keeps the offline and online semantics aligned.

```python
training_df = store.get_historical_features(
    entity_df=labels_df,             # driver_id + event_timestamp per label
    features=["driver_hourly_stats:conv_rate",
              "driver_hourly_stats:trips_today"],
).to_df()                            # each row joined as-of its event time
```

### Implementation sketch (Scala)

The as-of join is the load-bearing algorithm. Given feature rows sorted by timestamp per key, each entity row resolves in O(log n) by binary search for the predecessor, so a full join over *m* entity rows and *n* feature rows costs O(n log n + m log n) rather than the O(n·m) of a nested scan. `ttl` is applied after the search, not during it.

```scala
final case class FeatureRow(key: String, ts: Long, value: Double)
final case class EntityRow(key: String, ts: Long)

/** Newest feature row at or before `e.ts`, within `ttlMillis`. */
def asOfJoin(
    features: Seq[FeatureRow],
    entities: Seq[EntityRow],
    ttlMillis: Long
): Map[EntityRow, Option[Double]] =
  val byKey: Map[String, Vector[FeatureRow]] =
    features.groupBy(_.key).view.mapValues(_.sortBy(_.ts).toVector).toMap

  entities.map { e =>
    val rows = byKey.getOrElse(e.key, Vector.empty)
    // insertion point of e.ts; predecessor is the last row with ts <= e.ts
    val i = rows.search(FeatureRow(e.key, e.ts, 0.0))(Ordering.by[FeatureRow, Long](_.ts)) match
      case scala.collection.Searching.Found(j)          => j
      case scala.collection.Searching.InsertionPoint(j) => j - 1

    val candidate = if i >= 0 then Some(rows(i)) else None
    e -> candidate.filter(r => e.ts - r.ts <= ttlMillis).map(_.value)
  }.toMap
```

Two properties are worth naming. Ties at exactly `e.ts` resolve to the feature row, since the condition is *at or before*; changing it to strictly-before would discard a value the serving path would have seen. And a missing value is `None` rather than an omitted row: dropping unmatched entity rows would silently shrink the training set and bias it toward well-covered entities.

## Materialization and freshness

**Materialization** is the batch job that copies feature values from the offline store into the online store so they become serveable. **Freshness** is the interval between a change in the world and the online store reflecting it; it is set per feature by the materialization schedule.

```bash
feast apply                                  # register definitions
feast materialize-incremental $(date -u +%Y-%m-%dT%H:%M:%S)
```

`materialize-incremental` advances from the previously recorded position to the supplied end timestamp, so repeated runs process only new rows. At serving time the application reads latest values keyed by entity:

```python
features = store.get_online_features(
    features=["driver_hourly_stats:conv_rate",
              "driver_hourly_stats:trips_today"],
    entity_rows=[{"driver_id": 1004}],
).to_dict()
```

| Concern | Offline store | Online store |
| --- | --- | --- |
| Purpose | Training / batch scoring | Real-time serving |
| Contents | Full timestamped history | Latest value per entity |
| Access pattern | Large point-in-time joins | Single-key lookups |
| Freshness | As of last materialize | As of last materialize/push |
| Typical backend | BigQuery, Snowflake, parquet | Redis, DynamoDB, PostgreSQL |

For features that must reflect events seconds old — fraud scoring, dynamic pricing — batch materialization on a schedule cannot meet the requirement, because freshness is bounded below by the schedule interval. A streaming push to the online store makes freshness event-driven instead.

## Pitfalls

- **Equality join on the entity key instead of an as-of join.** Symptom: offline evaluation metrics far exceed production metrics, with no error anywhere. Cause: the join admits feature rows whose timestamp postdates the labeled event, so the model trains on data unavailable at inference.
- **`ttl` shorter than the materialization interval.** Symptom: online reads return nulls between materialization runs. Cause: the materialized value expires before the next run replaces it, so the serving path sees a gap the training path never contained.
- **`ttl` longer than the feature is meaningful.** Symptom: training rows carry values from days before the event, and the model learns from a staler signal than production supplies. Cause: the as-of scan is permitted to reach back that far.
- **Entity dataframe timestamps recorded at label-write time rather than event time.** Symptom: skew proportional to labeling delay. Cause: the as-of cutoff is taken from the wrong clock, admitting features computed after the event occurred.
- **Batch materialization used for a second-scale feature.** Symptom: the feature is systematically stale in production while appearing current in training. Cause: freshness is bounded below by the cron interval; the fix is a streaming push, not a shorter cron.
- **A serving path that recomputes a feature locally "for speed".** Symptom: skew reappears for exactly that feature after the store was adopted. Cause: the single-definition invariant is broken, and nothing in the system detects a second implementation.
