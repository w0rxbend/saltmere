---
title: "Ingesting ESP32 air-quality data into InfluxDB 3 with line protocol"
date: 2026-08-11
track: iot-embedded
summary: "InfluxDB 3 Core reached general availability in April 2025 on a columnar engine built from Arrow, DataFusion and Parquet. This article covers writing PM2.5, VOC-index and NOx-index readings from an ESP32 to the /api/v3/write_lp endpoint, and reading them back with SQL."
reading_time: 7
tags: [esp32, influxdb, line-protocol, time-series, air-quality, sql]
sources:
  - title: "InfluxDB 3 Core & Enterprise GA (InfluxData blog, Apr 15 2025)"
    url: "https://www.influxdata.com/blog/influxdb-3-oss-ga/"
  - title: "Use the v3 write_lp API to write data — InfluxDB 3 Core docs"
    url: "https://docs.influxdata.com/influxdb3/core/write-data/http-api/v3-write-lp/"
  - title: "Line protocol reference — InfluxDB 3 Core docs"
    url: "https://docs.influxdata.com/influxdb3/core/reference/line-protocol/"
  - title: "influxdb3 query CLI reference — InfluxDB 3 Core docs"
    url: "https://docs.influxdata.com/influxdb3/core/reference/cli/influxdb3/query/"
---

**Gist.** A sensor node emits readings continuously, but a microcontroller has no place to keep history and no query engine; the readings must land in a time-series store. InfluxDB 3 accepts them as **line protocol**, a newline-delimited text format posted over HTTP, which costs the node one `snprintf` per reading and no serialization library. The cost is that the format carries the schema implicitly — measurement, tag set, field set, timestamp — so type drift, timestamp-precision mismatch and tag-cardinality mistakes are detected at the server or not at all.

The [SEN5x node described earlier](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt) produces PM2.5, volatile organic compound (VOC) index, nitrogen-oxide (NOx) index, temperature and humidity every few seconds. Message Queuing Telemetry Transport (MQTT) is the appropriate seam for a fleet; the storage layer downstream is a separate concern, and for time series that layer is frequently InfluxDB.

## The engine as of the 3.x generation

InfluxData shipped **InfluxDB 3 Core and Enterprise to general availability on 15 April 2025**. Core is the free single-node open-source build; Enterprise adds clustering and high availability. The rewrite replaces the earlier Time-Structured Merge tree (TSM) storage engine with the **FDAP stack**: Apache **F**light for wire transport, **D**ataFusion as the query engine, **A**rrow as the in-memory columnar representation, and **P**arquet as the on-disk persistence format. Two consequences follow. Data is stored columnarly in Parquet files rather than in TSM's series-keyed layout, and **queries are expressed in SQL** rather than only in Flux or InfluxQL.

None of this alters the ingest path. Writes remain an HTTP POST carrying line protocol, which is the cheapest payload a microcontroller can construct.

## Line protocol

The grammar is `measurement,tag_set field_set timestamp`:

```
air,room=office,sensor=sen55 pm25=7.3,voc=142i,nox=18i 1754899200000000000
```

Read left to right:

- `air` — the **measurement**, analogous to a table name.
- `room=office,sensor=sen55` — **tags**, comma-separated with no spaces. Tag values are always strings, and tags are the columns used for filtering and grouping.
- A **single space** separates the tag set from the field set. This space is the only structural delimiter between the two sections, which is why an unescaped space inside a tag value corrupts the parse rather than producing a wrong value.
- `pm25=7.3,voc=142i,nox=18i` — **fields**, the measured values. A bare number is a float; an `i` suffix denotes an integer; strings are double-quoted; booleans are `t` or `f`.
- `1754899200000000000` — the **timestamp**. Default precision is nanoseconds since the Unix epoch. Omitting it causes the server to stamp arrival time, which on a queued node records the time of delivery rather than the time of measurement.

Spaces, commas and equals signs appearing inside tag or field keys and values must be backslash-escaped. Restricting tag values to bounded identifiers (`office`, not `Conference Room, 2nd floor`) avoids the escaping path entirely.

## Writing from the ESP32

The v3 write endpoint is **`POST /api/v3/write_lp`**, with a required `db` query parameter and an optional `precision` taking `auto`, `nanosecond`, `microsecond`, `millisecond` or `second`. Authentication is a bearer token. The batching protocol is nothing more than concatenation: **one line per reading, separated by `\n`, in a single request body.**

```cpp
#include <WiFi.h>
#include <HTTPClient.h>

const char* INFLUX_URL =
  "http://influx.lan:8181/api/v3/write_lp?db=airquality&precision=millisecond";
const char* INFLUX_TOKEN = "apiv3_xxx...";   // store in NVS, not in source

// Build one line; ts_ms is epoch milliseconds, matching precision=millisecond
size_t appendLine(char* out, size_t cap,
                  const char* room, float pm25, int voc, int nox, uint64_t ts_ms) {
  return snprintf(out, cap,
    "air,room=%s,sensor=sen55 pm25=%.1f,voc=%di,nox=%di %llu\n",
    room, pm25, voc, nox, (unsigned long long)ts_ms);
}

bool writeBatch(const char* body) {
  HTTPClient http;
  http.begin(INFLUX_URL);
  http.addHeader("Authorization", String("Bearer ") + INFLUX_TOKEN);
  http.addHeader("Content-Type", "text/plain; charset=utf-8");
  int code = http.POST((uint8_t*)body, strlen(body));
  http.end();
  return code == 204;             // 204 No Content == accepted
}
```

A successful write returns **`204 No Content`**. A `400` indicates malformed line protocol, most often an escaping error or a type mismatch — a field written as a float on one request and as an integer on the next, since the field type is fixed by the first write and the second no longer parses into it.

The `precision` parameter and the emitted timestamps are a single coupled decision. The server interprets the integer literal in the unit named by `precision`; it has no way to infer the intended unit from magnitude. **Sending nanosecond-scale integers under `precision=millisecond` multiplies the instant by 10^6**, placing the points far outside any plausible query window while the write still returns `204`. The failure is silent at ingest and appears only as an empty result set.

The equivalent two-point batch from a workstation:

```bash
curl "http://influx.lan:8181/api/v3/write_lp?db=airquality&precision=millisecond" \
  --header "Authorization: Bearer apiv3_xxx..." \
  --data-binary $'air,room=office,sensor=sen55 pm25=7.3,voc=142i,nox=18i 1754899200000\nair,room=lab,sensor=sen55 pm25=11.9,voc=210i,nox=31i 1754899200000'
```

## Batching and loss of the link

Posting once per sample pays a full connection setup — a Transport Layer Security (TLS) handshake where TLS is in use, plus an HTTP round-trip — for a payload of a few dozen bytes. The documentation's guidance is to **batch writes** and emit them on a steady interval rather than trickling single points; folding many readings into one body reduces both radio-on time on the node and request count on the server. The documentation does not state a batch size for a constrained node, so the interval is set by how much data the node can afford to lose to a reset.

The harder case is the link disappearing mid-batch. This is the [store-and-forward arrangement described in the LittleFS article](/articles/iot-embedded/2026-07-31-littlefs-store-and-forward), and line protocol fits it more directly than JSON does. Each reading is appended as one line to a log file on LittleFS; on a `204` the node advances a persisted byte-offset cursor. Because the format is already newline-delimited, **the on-flash queue file is itself a valid request body** and requires no re-serialization before transmission.

The invariant that makes replay safe is the identity of a point: **measurement, tag set and timestamp together identify a row; a later point with that same identity replaces the earlier one rather than adding a second row.** A node that reboots after a successful write but before the cursor is durably updated will resend the same lines, and the second write leaves the stored data unchanged. At-least-once delivery therefore yields one stored point per identity, provided the node emits its own timestamps — under server-assigned arrival timestamps each replay carries a new instant and duplicates.

## Tag cardinality

The TSM engine indexed data by series key, so each distinct tag combination cost memory in the index. The columnar Parquet layout of InfluxDB 3 is more tolerant of wide tag sets, but the distinction still governs query performance and file layout. The rule: **tags are the columns used to filter and group; fields are the measured values.**

| Put in a **tag** | Put in a **field** |
|---|---|
| `room=office`, `sensor=sen55`, `device=node-07` | `pm25`, `voc`, `nox`, `temp` |
| bounded, low-churn label sets | continuously changing numbers |
| anything in a `WHERE`/`GROUP BY` | anything passed to `AVG()`/`MAX()` |

Placing a value that is unique per reading — a request identifier, a raw timestamp, a monotonic counter — into a tag creates one series per reading. A device identifier and a location are bounded; a per-sample universally unique identifier (UUID) is not.

## Reading it back with SQL

Under DataFusion the measurement is a table and tags and fields are columns. The `influxdb3` command-line interface issues queries directly:

```bash
influxdb3 query --database airquality --token apiv3_xxx... \
  "SELECT room, avg(pm25) AS pm25_avg
   FROM air
   WHERE time > now() - INTERVAL '1 hour'
   GROUP BY room
   ORDER BY room"
```

The same statement runs over the HTTP query API for a dashboard or backend service.

## Retention

A retention period is set per database, and the window must be chosen deliberately, because Parquet files otherwise accumulate without bound. Where long-horizon trends are required, a scheduled downsample into a second measurement — hourly averages, for example — preserves the trend at a fraction of the row count that retaining every few-second sample would demand.

## Pitfalls

- **A field written first as a float and later as an integer produces `400` on the later write.** The field's type is fixed by the first accepted point; `pm25=7` and `pm25=7.0` are not interchangeable once the column exists as a float.
- **Timestamps whose unit disagrees with the `precision` parameter are accepted with `204` and then never appear in queries.** The server scales the literal by the declared unit; there is no magnitude check to reject an implausible instant.
- **An unescaped space in a tag value splits the line at the wrong point.** The parser treats the first unescaped space as the tag-set/field-set boundary, so `room=Conference Room` makes `Room` the start of the field set and the line fails to parse.
- **Omitting the timestamp defeats replay idempotency.** With server-assigned arrival times, a resent batch after a reboot carries different instants and is stored as additional points rather than overwriting the originals.
- **A per-sample unique value in a tag creates a new series per reading.** Query planning and file layout degrade even though the columnar engine tolerates cardinality better than TSM did.
- **The bearer token compiled into firmware is readable from a flash dump.** Keeping it in non-volatile storage (NVS) rather than a string literal in source is what the code comment above refers to.
