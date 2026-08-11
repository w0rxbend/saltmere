---
title: "Ingesting ESP32 air-quality data into InfluxDB 3 with line protocol"
date: 2026-08-11
track: iot-embedded
summary: "InfluxDB 3 Core went GA in April 2025 on a new columnar engine — Arrow, DataFusion and Parquet. Here's how to push PM2.5/VOC/CO2 from an ESP32 straight into it over the /api/v3/write_lp endpoint, then query it back with SQL."
reading_time: 6
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

The [SEN5x node from the earlier post](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt) hands you PM2.5, VOC, NOx, temperature and humidity every few seconds. MQTT is the right seam for a *fleet*, but somewhere downstream those readings need to land in a store you can graph and query over time. For time-series that store is very often InfluxDB — and as of 2025 the current generation is a genuinely different engine than the one most tutorials still describe.

## What InfluxDB 3 actually is now

InfluxData shipped **InfluxDB 3 Core and Enterprise to GA on April 15, 2025** (Core is the free, single-node open-source build; Enterprise adds clustering and HA). The rewrite — developed under the internal codename "IOx" — replaces the old TSM storage engine with the **FDAP stack**: Apache **F**light for wire transport, Data**F**usion as the vectorized SQL engine, **A**rrow as the in-memory columnar format, and **P**arquet as the on-disk persistence format. The practical upshot for us: data is stored as Parquet, queried columnarly, and — the headline change from InfluxDB 1.x/2.x — **you query it with real SQL**, not just Flux or InfluxQL.

None of that changes how a microcontroller writes to it. Ingest is still HTTP POST of **line protocol**, which is the cheapest possible thing to emit from an ESP32.

## Line protocol in one line

The format is `measurement,tag_set field_set timestamp`:

```
air,room=office,sensor=sen55 pm25=7.3,voc=142i,co2=612i 1754899200000000000
```

Reading that left to right:

- `air` — the **measurement** (think table name).
- `room=office,sensor=sen55` — **tags**, comma-separated, no spaces. Tags are indexed strings; they're what you filter and group by.
- ` ` — a single space separates tags from fields.
- `pm25=7.3,voc=142i,co2=612i` — **fields**, the actual values. A bare number is a float; an `i` suffix makes it an integer. Strings go in double quotes, booleans are `t`/`f`.
- `1754899200000000000` — the **timestamp**. Default precision is nanoseconds since the Unix epoch. You can omit it and the server stamps arrival time, but on a sensor node you almost always want to send your own.

Watch the escaping rules: spaces, commas and equals signs inside tag or field keys/values must be backslash-escaped. Keep tag values clean (`office`, not `Conference Room, 2nd floor`) and you never hit that.

## Writing from the ESP32

The v3 write endpoint is **`POST /api/v3/write_lp`**, with a required `db` query parameter and an optional `precision` (`auto`, `nanosecond`, `microsecond`, `millisecond`, or `second`). Auth is a bearer token. Here's an Arduino-core `HTTPClient` POST that batches a few readings into one body — one line per reading, `\n`-separated, which is the whole batching protocol:

```cpp
#include <WiFi.h>
#include <HTTPClient.h>

const char* INFLUX_URL =
  "http://influx.lan:8181/api/v3/write_lp?db=airquality&precision=millisecond";
const char* INFLUX_TOKEN = "apiv3_xxx...";   // keep in NVS, not source

// Build one line; ts_ms is epoch milliseconds (precision=millisecond above)
size_t appendLine(char* out, size_t cap,
                  const char* room, float pm25, int voc, int co2, uint64_t ts_ms) {
  return snprintf(out, cap,
    "air,room=%s,sensor=sen55 pm25=%.1f,voc=%di,co2=%di %llu\n",
    room, pm25, voc, co2, (unsigned long long)ts_ms);
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

A successful write returns **`204 No Content`**. A `400` means malformed line protocol (usually an escaping or type mismatch — e.g. a field that was a float on one write and an integer on the next). Note `precision=millisecond` in the URL, matching the millisecond timestamps we emit; if you send nanosecond timestamps under a millisecond precision the points land tens of thousands of years away, so keep the two in sync.

Same thing from a workstation with `curl`, writing a two-point batch:

```bash
curl "http://influx.lan:8181/api/v3/write_lp?db=airquality&precision=ms" \
  --header "Authorization: Bearer apiv3_xxx..." \
  --data-binary $'air,room=office,sensor=sen55 pm25=7.3,voc=142i,co2=612i 1754899200000\nair,room=lab,sensor=sen55 pm25=11.9,voc=210i,co2=845i 1754899200000'
```

## Batching, backpressure and flaky links

Don't POST once per sample. Every write is a TLS handshake plus HTTP round-trip; batching 10–60 points into a single body cuts radio-on time and load on the server dramatically. The docs' own guidance is to **batch writes** and send them at a steady interval rather than trickling single points.

The harder problem is the link going away mid-batch. This is exactly the [store-and-forward pattern from the LittleFS post](/articles/iot-embedded/2026-07-31-littlefs-store-and-forward), and line protocol makes it even cleaner than JSON: append each reading as one line to a LittleFS log, and on a successful `204` advance a persisted byte-offset cursor. Because line protocol is already newline-delimited, your on-flash queue file *is* a ready-to-POST request body — no re-serialization. Writes are idempotent by design: re-sending a point with the same measurement, tags and timestamp overwrites rather than duplicates, so an at-least-once replay after a reboot is safe.

## Tag cardinality: the one pitfall that bites at scale

The old TSM engine punished high tag cardinality brutally. InfluxDB 3's columnar/Parquet design is far more forgiving, but the discipline still matters for query performance and file layout. The rule: **tags are for things you filter and group by; fields are for measured values.**

| Put in a **tag** | Put in a **field** |
|---|---|
| `room=office`, `sensor=sen55`, `device=node-07` | `pm25`, `voc`, `co2`, `temp` |
| bounded, low-churn label sets | continuously changing numbers |
| anything in a `WHERE`/`GROUP BY` | anything you `AVG()`/`MAX()` |

The classic mistake is putting a unique-per-reading value (a request id, a raw timestamp, a monotonic counter) in a tag — each distinct value spawns a new series. Device id and location are fine; a UUID per sample is not.

## Reading it back with SQL

Because DataFusion is a SQL engine, the measurement is just a table and tags/fields are columns. Query with the `influxdb3` CLI:

```bash
influxdb3 query --database airquality --token apiv3_xxx... \
  "SELECT room, avg(pm25) AS pm25_avg
   FROM air
   WHERE time > now() - INTERVAL '1 hour'
   GROUP BY room
   ORDER BY room"
```

The same SQL works over the HTTP query API for a dashboard or backend service. Grafana talks to InfluxDB 3 over FlightSQL, so those columns drop straight into panels.

## Retention

Core keeps data in a single database-level retention window (Enterprise adds more granular control). Decide it up front: a home node graphing indoor air quality rarely needs raw-resolution points older than a few weeks. Set a retention period so Parquet files age out automatically instead of growing unbounded, and if you need long-term trends, downsample into a second measurement (hourly averages) on a schedule rather than keeping every 5-second sample forever.

**Try next:** stand up InfluxDB 3 Core locally (`docker run` the official image), point one SEN5x node at `/api/v3/write_lp`, then pull its Wi-Fi for ten minutes and confirm the LittleFS queue replays the gap on reconnect with no duplicate points — then run the `avg(pm25) GROUP BY room` query and watch the gap fill in.
