---
title: "SQL vs NoSQL: modelling by access pattern"
date: 2026-08-10
track: microservices
summary: "The claim that NoSQL scales and SQL does not is wrong and dated; the operative question is which data model fits the access patterns. This covers the four NoSQL families and the pattern each serves, the trade between relational ad-hoc joins and ACID versus NoSQL horizontal scale and data locality, the distributed-SQL middle, and the same data modelled relationally versus as a query-first Cassandra table."
reading_time: 7
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

**Gist.** Framing the choice between relational and NoSQL (non-relational) stores as a scaling contest hides the actual decision: which data model the read and write patterns require. Relational stores keep each fact in one place and answer unanticipated questions by joining at read time; NoSQL stores shape storage around a fixed set of queries, so a query becomes a single located read. The cost of that locality is that the set of cheap queries is frozen at design time — a new query means a new table, new writes, and duplicated facts that can drift.

## "NoSQL" denotes four unrelated designs

The term groups storage engines that share nothing beyond not being a classic relational store. Kleppmann's Chapter 2 splits data models by the shape they impose on data. Four families recur, each aligned to one access pattern.

**Key-value stores (Redis, DynamoDB, Memcached).** A hash map addressed over the network: a value is written under a key and retrieved by that key, and the store does not query the value's contents. Durability varies across the family rather than defining it — DynamoDB persists, Memcached is purely in-memory and loses its contents on restart. The matching access pattern is one where **the key is already known** — sessions, feature flags, shopping carts, caches.

**Document stores (MongoDB, Couchbase, DynamoDB).** Self-contained JSON-like documents. The property gained is **data locality**: a document nesting a user with addresses and recent orders is retrieved in one read, with no join, and the schema may vary between documents. The property lost is shared reference — data referenced from many places, such as a product appearing in a thousand carts, is either duplicated in each document or split out and joined by the application. Many-to-many relationships work against the model.

**Wide-column stores (Cassandra, HBase, ScyllaDB, Bigtable).** Not tables with many columns: rows are grouped into **partitions** by a partition key and ordered within a partition by **clustering keys**. Writes are appended (log-structured merge-tree engines), which supports high write volume, and capacity grows by adding nodes. The constraint is discussed below.

**Graph databases (Neo4j, JanusGraph).** Nodes and edges are first-class. Where the workload is multi-hop many-to-many traversal — friends-of-friends, fraud rings, dependency graphs — traversal replaces a chain of self-joins.

Naming the family and the pattern it serves carries the information; naming "NoSQL" does not.

## The trade-off

| Dimension | Relational (Postgres, MySQL) | NoSQL (varies by family) |
|---|---|---|
| Query flexibility | High — ad-hoc queries, joins across normalized tables | Low — fast only on the access patterns designed for |
| Schema | Fixed, enforced, migrated | Flexible / schema-on-read |
| Normalization | Normalized; single source of truth avoids update anomalies | Denormalized; data duplicated across query-shaped tables |
| Consistency | Strong ACID transactions | Often tunable / eventual (see PACELC) |
| Joins | First-class | Usually none — joined in the application or pre-joined at write time |
| Horizontal scale | Harder (though improving) | Native, via partitioning |
| Best when | Access patterns are unknown or change often | One or few known, high-volume access patterns |

The relational properties are structural, not stylistic. **Normalization places a fact in exactly one row**, so no update can leave two copies disagreeing; that is the update anomaly which denormalized stores reintroduce deliberately. **Joins answer questions not anticipated at schema-design time**, which is most questions in a young system. **ACID transactions supply multi-row atomicity** without an application-level protocol.

The NoSQL properties are equally structural. **Horizontal scale through partitioning** (see [data partitioning and sharding](/articles/distributed-systems/2026-08-10-data-partitioning-sharding)) is the visible one; **data locality** — arranging storage so one access pattern is a single contiguous read — is the quieter one. AWS states the trade directly: a relational database management system permits a schema designed for flexibility without regard to performance, whereas a DynamoDB schema is designed specifically to make the most common and important queries as fast and as inexpensive as possible.

## One dataset, two models

Consider `users` and `orders`, and the query "the most recent orders of one user".

**Relational.** Two normalized tables, joined and sorted at read time:

```sql
CREATE TABLE users  (user_id BIGINT PRIMARY KEY, name TEXT);
CREATE TABLE orders (order_id BIGINT PRIMARY KEY,
                     user_id BIGINT REFERENCES users,
                     created_at TIMESTAMPTZ, total_cents INT);

SELECT o.order_id, o.created_at, o.total_cents
FROM orders o WHERE o.user_id = $1
ORDER BY o.created_at DESC LIMIT 20;
```

Each fact exists once. An unplanned query — orders per day, revenue per region — is another `SELECT` with no schema change. The join and the sort are paid at read time, and distributing `orders` across nodes is separate work.

**Cassandra (query-first).** The Cassandra documentation states that "data modeling is query-driven… data access patterns and application queries determine the structure." The unit of design is **one table per query**, with data duplicated across them: "denormalization… Data duplication and a high write throughput are used to achieve a high read performance."

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

`user_id` is the partition key, so one user's orders are colocated in a single partition; `created_at DESC` is the clustering key, so the rows are already stored in the required order. The query is a **single-partition read with no join and no sort**. The query "most recent orders across all users" has no efficient form against this table, because the partitioning does not group rows that way; serving it requires a second table, `orders_by_day`, and every order written to both. **New queries mean new tables and additional writes.**

The trade reduces to when the work is paid: the relational model pays at read time and keeps future questions cheap; the wide-column model pays at write time and in duplication to make one designated question close to free.

### Implementation sketch (Scala)

Query-first modelling makes the write path a fan-out: one domain event is projected into one row per query table. Expressing the projection as a single function collects the set of tables that must be written into one place, so a new query is added where the fan-out is already visible rather than at whichever call site happened to need it.

```scala
final case class Order(orderId: Long, userId: Long, createdAt: java.time.Instant, totalCents: Int)

/** A row destined for exactly one query table. */
sealed trait Row:
  def table: String
  def partitionKey: Any

final case class ByUser(userId: Long, createdAt: java.time.Instant, orderId: Long, totalCents: Int) extends Row:
  val table: String = "orders_by_user"
  val partitionKey: Any = userId

final case class ByDay(day: java.time.LocalDate, createdAt: java.time.Instant, orderId: Long, userId: Long) extends Row:
  val table: String = "orders_by_day"
  val partitionKey: Any = day

// Every supported query has its row built here; the write path never constructs rows elsewhere.
def project(o: Order): List[Row] =
  val day = o.createdAt.atZone(java.time.ZoneOffset.UTC).toLocalDate
  List(
    ByUser(o.userId, o.createdAt, o.orderId, o.totalCents),
    ByDay(day, o.createdAt, o.orderId, o.userId)
  )

// The rows of one order land in different partitions, hence in different atomic units:
// a failure after the first write leaves the tables disagreeing until the write is retried.
def writeAll(o: Order)(write: Row => Unit): Unit = project(o).foreach(write)
```

The final comment records the invariant the model does not provide: `project` guarantees the row set, not that the rows become visible together.

## Distributed SQL

The dichotomy is partly false. **Distributed SQL** systems — Google Spanner, CockroachDB, YugabyteDB, TiDB — retain the SQL interface and ACID transactions while partitioning data across nodes. (Vitess shards MySQL behind a SQL interface but does not offer the same cross-shard transactional guarantees by default, so it sits adjacent to the category rather than inside it.) Spanner uses TrueTime for externally consistent global transactions; CockroachDB layers SQL over a replicated, range-sharded key-value store. The coordination does not disappear: cross-partition transactions carry coordination latency, and under [CAP/PACELC](/articles/distributed-systems/2026-08-10-cap-theorem-pacelc) the consistency-versus-latency choice remains in the failure-free case. The claim that scaling writes requires abandoning SQL and joins no longer holds unconditionally.

## A decision order

1. **Enumerate the access patterns** — the concrete reads and writes. If they cannot be enumerated, the data model is not yet known, which argues for relational flexibility until the patterns stabilize.
2. **Determine whether one pattern needs horizontal scale.** Where a single pattern dominates at high throughput (event ingestion, timelines, telemetry), a purpose-built store — wide-column for write volume, key-value for point lookups — is warranted.
3. **Match the shape to a family.** Deeply nested aggregates suggest documents; many-to-many traversal suggests a graph; point lookups by key suggest key-value.
4. **Where both scale and multi-row transactions are required,** evaluate distributed SQL before manually sharding a relational database or emulating joins over a document store.

The default is relational; a NoSQL family is justified by a specific, known, high-volume access pattern traded against flexibility.

## Pitfalls

- **A second query added to a query-first table without a second table.** The symptom is a full-cluster scan or a rejected query; the cause is that the partition key groups rows for one query only, and no other grouping exists on disk.
- **Denormalized copies drifting apart.** The symptom is two views of the same order reporting different totals; the cause is that the copies live in different partitions, so their writes are not one atomic unit, and a failure between them is not rolled back.
- **A document model used for many-to-many data.** The symptom is an update touching thousands of documents; the cause is that a shared entity embedded in each parent has no single location to update.
- **A wide-column partition key with unbounded growth.** The symptom is one node's storage and latency diverging from the rest; the cause is that all rows for the key are colocated in a single partition by definition.
- **Choosing a store before enumerating the access patterns.** The symptom is later queries requiring application-side joins; the cause is that locality was purchased against patterns that had not been identified.
- **Assuming distributed SQL removes coordination cost.** The symptom is transaction latency rising with the number of partitions touched; the cause is that a cross-partition commit requires agreement among the participating replicas.
