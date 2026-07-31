---
title: "Store-and-forward on the ESP32: don't lose readings to a dropped network"
date: 2026-07-31
track: iot-embedded
summary: "A field air-quality node that drops its samples every time Wi-Fi hiccups is worse than useless — the gaps are exactly when something interesting happened. LittleFS gives you a power-loss-safe log on flash; here's a buffer-and-replay design that survives outages and reboots."
reading_time: 6
tags: [esp32, littlefs, mqtt, resilience, flash, esp-idf]
sources:
  - title: "Creating an environmental logger, Part 1 — Espressif Developer Portal (Jul 2026)"
    url: "https://developer.espressif.com/blog/2026/07/basic-logger/"
  - title: "joltwallet/esp_littlefs — LittleFS VFS component for ESP-IDF"
    url: "https://github.com/joltwallet/esp_littlefs"
  - title: "littlefs — a little fail-safe filesystem (design/README)"
    url: "https://github.com/littlefs-project/littlefs"
---

Your node reads a sensor every few seconds and publishes over MQTT. That works right up until the Wi-Fi drops, the broker restarts, or the uplink browns out — and now every reading taken during the gap is gone. For an air-quality node the outage window is often the *most* interesting data, so "publish or discard" is the wrong policy. The fix is store-and-forward: write every reading to local flash first, publish opportunistically, and replay whatever hasn't been acknowledged when the link returns.

## Why LittleFS for the buffer

LittleFS is a **log-structured, power-loss-resilient** filesystem with built-in wear leveling — designed exactly for microcontrollers that can lose power mid-write. That last property matters: a battery node *will* reset at the worst possible moment, and LittleFS is built so a half-finished write can't corrupt the whole filesystem the way FAT can. On ESP-IDF it's a drop-in VFS via the `joltwallet/littlefs` component (Espressif's July 2026 logger tutorial uses it directly).

Declare a partition and register the filesystem:

```
# partitions.csv
storage,  data, littlefs,  ,  0xF0000,
```

```c
esp_vfs_littlefs_conf_t conf = {
    .base_path = "/lfs",
    .partition_label = "storage",
    .format_if_mount_failed = true,
};
ESP_ERROR_CHECK(esp_vfs_littlefs_register(&conf));
```

From there it's plain POSIX — `fopen`, `fprintf`, `fread`.

## The design: append-only log + a persisted checkpoint

Keep it dead simple. Append each reading as one newline-delimited JSON record, and separately persist a single integer — the byte offset up to which records have been **acknowledged** by the broker. The log is the queue; the offset is the read cursor.

```c
// 1) On every sample: append. This never blocks on the network.
FILE *f = fopen("/lfs/queue.ndjson", "a");
fprintf(f, "{\"ts\":%lld,\"pm25\":%.1f,\"pm10\":%.1f}\n", now_ms(), pm25, pm10);
fclose(f);                         // fclose flushes; LittleFS keeps it crash-safe

// 2) When online: replay from the saved offset, publish, advance the cursor.
long sent = load_offset();         // read a small "/lfs/offset" file, default 0
FILE *q = fopen("/lfs/queue.ndjson", "r");
fseek(q, sent, SEEK_SET);
char line[192];
while (fgets(line, sizeof line, q)) {
    long pos = ftell(q);
    if (mqtt_publish_qos1(line) != OK) break;   // QoS 1 = broker acknowledged
    sent = pos;
    save_offset(sent);             // only advance AFTER the ack
}
fclose(q);
```

The rule that makes this correct: **advance the cursor only after the broker acknowledges (QoS 1).** If the node reboots after publishing but before saving the offset, it re-sends a few records — so make the consumer idempotent (a `ts` + device-id key deduplicates cheaply). At-least-once delivery plus a dedup key beats at-most-once-and-lose-data every time for telemetry.

## Keeping flash from filling up

Because it's append-only, the file grows forever unless you reclaim space. Two safe strategies. **Compaction:** once `sent` passes a threshold (say the file is >64 KB and fully drained), rewrite `queue.ndjson` to contain only the un-acked tail, then reset the offset to 0. **Segment rotation** is even simpler and gentler on flash: roll to `queue.0001.ndjson`, `queue.0002.ndjson`, … and `remove()` a segment the instant its last record is acknowledged — no rewriting, which means fewer erase cycles. On a small storage partition, rotation is usually the better call.

One more guard: if the outage outlives your flash budget, decide the drop policy on purpose. Dropping the *oldest* segment (and logging that you did) is almost always right for air-quality trends — you'd rather keep the recent picture than choke on ancient backlog. Silent overflow is the one outcome to avoid.

**Try next:** flash this, then pull the antenna / block the broker for ten minutes while the node keeps sampling. Reconnect and confirm the gap fills in from `queue.ndjson` with no missing timestamps — then yank power mid-replay and confirm it resumes from the saved offset instead of double-sending everything.
