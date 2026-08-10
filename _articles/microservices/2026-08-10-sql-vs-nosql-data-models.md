---
title: "SQL vs NoSQL: model by access pattern, not by hype"
date: 2026-08-10
track: microservices
summary: "The interview answer 'NoSQL scales, SQL doesn't' is wrong and dated. The real question is which data model fits your access patterns. This walks the four NoSQL families and what each is actually for, the honest trade — relational's ad-hoc joins and ACID versus NoSQL's horizontal scale and data locality — the NewSQL middle that refuses the dichotomy, and the same data modeled relationally versus as a query-first Cassandra table."
reading_time: 6
tags: [sql, nosql, data-modeling, cassandra, dynamodb, newsql]
sources:
  - title: "Kleppmann, Designing Data-Intensive Applications — Ch.2 Data Models and Query Languages (O'Reilly)"
    url: "https://www.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/"
  - title: "Apache Cassandra Documentation — Data Modeling: Introduction (query-driven design)"
    url: "https://cassandra.apache.org/doc/4.0/cassandra/data_modeling/intro.html"
  - title: "AWS DynamoDB Developer Guide — NoSQL Design for DynamoDB"
    url: "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-general-nosql-design.html"
  - title: "Abadi, Consistency Tradeoffs in Modern Distributed Database System Design (IEEE Computer, 2012)"
    url: "https://www.cs.umd.edu/~abadi/papers/abadi-pacelc.pdf"
  - title: "CockroachDB — What is Distributed SQL?"
    url: "https://www.cockroachlabs.com/glossary/distributed-db/distributed-sql/"
---

"SQL or NoSQL?" is a bad interview question with a good answer hiding inside it. The bad framing is a scaling contest — NoSQL wins throughput, SQL wins correctness, pick your religion. The good answer is that "NoSQL" is not one thing, relational databases scale further than people think, and the decision is really about **how you will read the data**, not about a logo. What follows is the version that survives follow-up questions.

## "NoSQL" is four different databases

The term lumps together storage engines with almost nothing in common except that they aren't a classic relational store. Kleppmann's Chapter 2 splits data models by the shape they impose; in practice you meet four families, and each exists for a specific access pattern.

**Key-value (Redis, DynamoDB, Memcached).** A hash map that survives a process restart. You put a value under a key and get it back by that key, in single-digit milliseconds, at essentially unlimited horizontal scale. There is no querying the value's contents by the store itself. Use it for sessions, feature flags, shopping carts, and caches — anywhere the access pattern is "I already know the key."

**Document (MongoDB, Couchbase, DynamoDB again).** Stores self-contained JSON-ish documents. The win is **data locality**: a document that nests a user with their addresses and recent orders is one read, no joins, and the schema can vary row to row. The cost is that data referenced from many places (a product in a thousand carts) is either duplicated or pulled apart — the moment you need many-to-many, the document model fights you.

**Wide-column (Cassandra, HBase, ScyllaDB, Bigtable).** Not "tables with lots of columns" — rows are grouped into **partitions** by a partition key and sorted within a partition by **clustering keys**. This is the throughput monster: writes are appended (LSM-tree engines under the hood), so it ingests enormous write volume and scales linearly by adding nodes. The price is rigidity, covered below.

**Graph (Neo4j, JanusGraph).** Nodes and edges as first-class citizens. When your workload is many-hop, many-to-many traversal — "friends of friends who liked X," fraud rings, dependency graphs — a graph engine turns what would be a pile of self-joins into a cheap walk of pointers.

The lesson interviewers want: naming "NoSQL" tells them nothing. Naming *which family and why* tells them you've used one.

## The honest trade-off table

| Dimension | Relational (Postgres, MySQL) | NoSQL (varies by family) |
|---|---|---|
| Query flexibility | High — ad-hoc queries, joins across normalized tables | Low — fast only on the access patterns you designed for |
| Schema | Fixed, enforced, migrated | Flexible / schema-on-read |
| Normalization | Normalized; single source of truth avoids update anomalies | Denormalized; data duplicated across query-shaped tables |
| Consistency | Strong ACID transactions | Often tunable / eventual (see PACELC) |
| Joins | First-class | Usually none — you join in the app or pre-join at write time |
| Horizontal scale | Harder (though improving) | Native, via partitioning |
| Best when | Access patterns are unknown or change often | One or few known, high-volume access patterns |

The relational strengths are underrated in interviews. **Normalization** means a fact lives in exactly one place, so an update can't leave two rows disagreeing — the update-anomaly problem denormalized stores reintroduce on purpose. **Joins** let you answer questions you didn't anticipate at schema-design time, which is most questions in a young product. **ACID** gives you multi-row atomicity without hand-rolling it.

The NoSQL strengths are equally real. **Horizontal scale via partitioning** (see [data partitioning and sharding](/articles/distributed-systems/2026-08-10-data-partitioning-sharding)) is the headline, but the quieter win is **data locality**: shaping storage so one access pattern is a single, tight read. The cost is symmetric — you trade the flexibility you had for the speed you designed. AWS states it plainly: an RDBMS lets you "design for flexibility without worrying about performance," while in DynamoDB "you design your schema specifically to make the most common and important queries as fast and as inexpensive as possible."

## Same data, two models

Take an app with `users` and `orders`, and the query "show a user's most recent orders."

**Relational.** Normalize into two tables and join at read time:

```sql
CREATE TABLE users  (user_id BIGINT PRIMARY KEY, name TEXT);
CREATE TABLE orders (order_id BIGINT PRIMARY KEY,
                     user_id BIGINT REFERENCES users,
                     created_at TIMESTAMPTZ, total_cents INT);

SELECT o.order_id, o.created_at, o.total_cents
FROM orders o WHERE o.user_id = $1
ORDER BY o.created_at DESC LIMIT 20;
```

One copy of each fact. Tomorrow's unplanned query — orders by day, revenue per region — is another `SELECT`, no schema change. The join and the sort cost you at read time, and spreading `orders` across nodes is work.

**Cassandra (query-first).** The Cassandra docs are blunt: "data modeling is query-driven… data access patterns and application queries determine the structure." You design **one table per query** and duplicate data — "denormalization… Data duplication and a high write throughput are used to achieve a high read performance."

```sql
CREATE TABLE orders_by_user (
  user_id     bigint,
  created_at  timestamp,
  order_id    bigint,
  total_cents int,
  PRIMARY KEY ((user_id), created_at, order_id)
) WITH CLUSTERING ORDER BY (created_at DESC);

SELECT * FROM orders_by_user WHERE user_id = ? LIMIT 20;
```

`user_id` is the partition key, so one user's orders live together on one node; `created_at DESC` is the clustering key, so "most recent" is already sorted on disk. That query is a single-partition read with no join and no sort — brutally fast, linearly scalable. But now ask "most recent orders across *all* users" and you're stuck: there is no efficient query for it, because you didn't build a table for it. You'd create `orders_by_day` and write every order to both tables. **You model for your queries, and new queries mean new tables and new writes.**

That's the whole trade in one screen: relational pays at read time to keep every future question cheap; Cassandra pays at write time and in duplication to make *this* question nearly free.

## The NewSQL / distributed-SQL middle

The dichotomy is now partly false. **Distributed SQL** systems — Google Spanner, CockroachDB, YugabyteDB, TiDB, and (via sharded MySQL) Vitess — keep the SQL interface and ACID transactions while partitioning data across many nodes for horizontal scale. Spanner uses TrueTime for externally consistent global transactions; CockroachDB layers SQL over a replicated, range-sharded key-value store. They aren't magic: cross-partition transactions still cost coordination latency, and by [CAP/PACELC](/articles/distributed-systems/2026-08-10-cap-theorem-pacelc) you still choose consistency-versus-latency in the normal case. But "you must abandon SQL and joins to scale writes" is no longer automatically true, and saying so signals you've kept up.

## The decision framework

Answer these, in order:

1. **What are the access patterns?** List the actual reads and writes. If you can't, you don't yet know the data is unknown — which is itself an argument for relational's flexibility. Start relational until the patterns stabilize.
2. **Is any one pattern high-volume enough to need horizontal scale?** If a single access pattern dominates at massive throughput (event ingestion, timelines, telemetry), a purpose-built store — wide-column for write volume, key-value for point lookups — earns its keep.
3. **Does the shape scream a family?** Deeply nested aggregates → document. Many-to-many traversal → graph. Point lookups by key → key-value.
4. **Do you need both scale and rich transactions?** Look at distributed SQL before hand-sharding a relational database or contorting a document store into joins.

Default to relational. Reach for a NoSQL family when a specific, known, high-scale access pattern justifies trading flexibility for locality — and be able to name the pattern, not the trend.

**Try next:** take the same `orders_by_user` model and add a second query — "orders in a given status for a user" — then decide whether it's a new Cassandra table, a clustering-key change, or a secondary index, and articulate why each choice costs what it costs.
