---
title: "Feature stores: killing train/serve skew with online + offline stores"
date: 2026-08-13
track: sys-patterns
summary: "A feature store is one feature definition serving two stores: an offline store for point-in-time-correct training data and an online store for low-latency serving. That symmetry is what eliminates train/serve skew. Here's the pattern, with Feast as the concrete example."
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

The most expensive bug in production ML is silent: a feature computed one way in the training pipeline and a subtly different way in the serving path. The model looked great offline and degrades in production, and nobody gets an exception. This is **train/serve skew**, and the feature store pattern exists to make it structurally impossible — one feature definition, materialized into two stores that share the same computation. Uber's Michelangelo popularized the split; Feast is the open-source distillation of it (current release **v0.64.0**, June 2026).

## Two stores, one definition

- **Offline store** (warehouse/parquet: BigQuery, Snowflake, files): full history of every feature value with timestamps. Feeds training and batch scoring. Optimized for large point-in-time joins, not latency.
- **Online store** (low-latency KV: Redis, DynamoDB, Postgres): only the *latest* value per entity. Serves models at inference in single-digit milliseconds.

The same `FeatureView` describes both. You write the transformation once; Feast handles landing it in each store. Skew dies because training and serving read features derived from one source of truth.

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

## Point-in-time correctness: the hard part of training

You cannot build a training set with a naive join on `driver_id` — that leaks the future. If you label an event at 10:00 but attach a feature value computed at 10:05, the model trains on information it will never have at inference. A feature store's core trick is the **point-in-time (as-of) join**: for each labeled event, select the newest feature row whose timestamp is *at or before* the event, and no older than the feature's `ttl`.

```python
training_df = store.get_historical_features(
    entity_df=labels_df,             # has driver_id + event_timestamp
    features=["driver_hourly_stats:conv_rate",
              "driver_hourly_stats:trips_today"],
).to_df()                            # each row joined as-of its event time
```

Get this wrong and your offline metrics are fiction. Get it right and offline evaluation actually predicts online behavior.

## Materialization and freshness

**Materialization** is the batch job that pushes feature values from the offline store into the online store so they're serveable. Freshness is the lag between when the world changes and when the online store reflects it — the knob you tune per feature.

```bash
feast apply                                  # register definitions
feast materialize-incremental $(date -u +%Y-%m-%dT%H:%M:%S)
```

At serving time the app reads only the latest values, keyed by entity:

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
| Access pattern | Large point-in-time joins | Single-key, sub-ms lookups |
| Freshness | As of last materialize | As of last materialize/push |
| Typical backend | BigQuery, Snowflake, parquet | Redis, DynamoDB, Postgres |

For features that must reflect events seconds old (fraud, dynamic pricing), batch materialization isn't enough — use a streaming push to the online store so freshness is driven by events, not a cron schedule.

**Try next:** Take one model's serving code, replace its ad-hoc feature SQL with a `get_online_features` call, and rebuild its training set with `get_historical_features`; diff the two feature distributions to quantify the skew you were shipping.
