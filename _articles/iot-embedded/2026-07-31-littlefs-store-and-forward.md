---
title: "Store-and-forward on the ESP32 with LittleFS"
date: 2026-07-31
track: iot-embedded
summary: "A field air-quality node that discards samples whenever Wi-Fi drops loses precisely the interval of interest. LittleFS provides a power-loss-resilient log on flash; this article develops a buffer-and-replay design that survives outages and resets."
reading_time: 7
tags: [esp32, littlefs, mqtt, resilience, flash, esp-idf]
sources:
  - title: "Creating an environmental logger, Part 1 — Espressif Developer Portal (Jul 2026)"
    url: "https://developer.espressif.com/blog/2026/07/basic-logger/"
  - title: "joltwallet/esp_littlefs — LittleFS VFS component for ESP-IDF"
    url: "https://github.com/joltwallet/esp_littlefs"
  - title: "littlefs — a little fail-safe filesystem (design/README)"
    url: "https://github.com/littlefs-project/littlefs"
---

**Gist.** A sensor node that publishes each reading directly over Message Queuing Telemetry Transport (MQTT) loses every sample taken while the link is down, and on an air-quality node the outage interval is often the interval worth keeping. Store-and-forward inverts the order: each reading is appended to a local log on flash first, published opportunistically, and replayed from a persisted cursor once the link returns. The cost is bounded flash capacity, a retention policy that must decide explicitly what to discard when the backlog exceeds it, and duplicate deliveries that the consumer must absorb.

## Why LittleFS holds the buffer

LittleFS is a **log-structured, power-loss-resilient** filesystem with wear levelling, written for microcontrollers that can lose power mid-write. The relevant property for a battery node is that an interrupted write does not leave the filesystem in an inconsistent state the way a partially written File Allocation Table (FAT) directory entry can: LittleFS commits metadata changes atomically, so an unexpected reset yields either the state before the commit or the state after it. On ESP-IDF (Espressif IoT Development Framework) it is available as a Virtual File System (VFS) driver through the `joltwallet/littlefs` component, which the Espressif environmental-logger tutorial of July 2026 uses directly.

A dedicated data partition is declared in the partition table, then mounted:

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

Note that `format_if_mount_failed` converts an unmountable partition into an empty one. That is convenient during development and destructive in the field: a transient mount failure erases the entire backlog. Production firmware should distinguish first boot from a mount error and report the latter.

After registration the interface is ordinary Portable Operating System Interface (POSIX) file input/output — `fopen`, `fprintf`, `fgets`, `fseek` — through the VFS layer.

## Design: append-only log plus a persisted cursor

The whole mechanism is two objects. **The log** is a single file of newline-delimited JavaScript Object Notation (NDJSON) records, one record per reading, only ever appended to. **The cursor** is a small separate file holding one integer: the byte offset up to which records have been acknowledged by the broker. The log is the queue; the cursor is the read position. No index, no per-record state flags, and therefore no way for the two to disagree beyond a bounded suffix.

```c
// 1) On every sample: append. This path never blocks on the network.
FILE *f = fopen("/lfs/queue.ndjson", "a");
fprintf(f, "{\"ts\":%lld,\"pm25\":%.1f,\"pm10\":%.1f}\n", now_ms(), pm25, pm10);
fclose(f);                         // flushes the buffered record to the filesystem

// 2) When online: replay from the saved offset, publish, advance the cursor.
long sent = load_offset();         // read "/lfs/offset"; absent file means 0
FILE *q = fopen("/lfs/queue.ndjson", "r");
fseek(q, sent, SEEK_SET);
char line[192];
while (fgets(line, sizeof line, q)) {
    long pos = ftell(q);
    if (mqtt_publish_qos1(line) != OK) break;   // QoS 1 = broker acknowledged
    sent = pos;
    save_offset(sent);             // advance only after the acknowledgement
}
fclose(q);
```

### The invariant

**Every record at a byte offset below `sent` has been acknowledged by the broker.** The cursor is written after the acknowledgement and never before, which makes the invariant one-sided: `sent` may lag behind what has been delivered, but it can never run ahead of it. All the correctness of the scheme follows from that asymmetry.

`mqtt_publish_qos1` in the sketch stands for a call that returns only once the `PUBACK` for that message has arrived, and no such call exists in ESP-IDF. `esp_mqtt_client_publish` hands the message to the client task and returns a message identifier; the acknowledgement surfaces later as an `MQTT_EVENT_PUBLISHED` event carrying the same identifier. Firmware has to correlate the two itself, because **treating the return of the publish call as the acknowledgement moves the cursor at enqueue time and converts the scheme into at-most-once.**

The three states of the system are: *draining* (cursor behind end-of-file, link up), *drained* (cursor at end-of-file), and *accumulating* (link down, appends continuing). A publish failure exits the replay loop without advancing the cursor, so the transition from draining to accumulating loses nothing. `fgets` truncates at `sizeof line`; a record longer than the buffer is split across two iterations and published as two malformed fragments, so the buffer must exceed the maximum record length by construction.

### The failure mode the invariant admits

If the node resets after `mqtt_publish_qos1` returns but before `save_offset` completes, the record is delivered again on the next replay. The window is one record wide per reset, and it is the deliberate price of the one-sided invariant. The system therefore provides **at-least-once delivery**, and **the consumer must be idempotent**: a primary key of device identifier plus record timestamp deduplicates on insert. Reversing the order — persisting the cursor first, publishing second — would give at-most-once delivery and silently drop the record instead, which for telemetry is the worse of the two errors because it is undetectable downstream.

Note what QoS 1 does and does not mean. The acknowledgement (`PUBACK`) confirms that the broker took responsibility for the message, not that any subscriber consumed it. Loss beyond the broker is outside what this cursor can protect against.

## Reclaiming flash

An append-only log grows without bound, so space must be reclaimed. Two strategies fit the design.

**Compaction** rewrites `queue.ndjson` to contain only the unacknowledged tail once the file exceeds a size threshold, then resets the cursor to 0. It keeps a single file, at the cost of rewriting live data and consuming erase cycles proportional to the retained tail.

**Segment rotation** writes to `queue.0001.ndjson`, `queue.0002.ndjson`, and so on, and calls `remove()` on a segment as soon as its final record is acknowledged. Nothing is rewritten, so the erase cost is lower, and the cursor becomes a pair — segment identifier plus offset within it. The trade is more filesystem objects to enumerate at boot against fewer rewritten bytes; no published measurement separates the two on a specific partition size.

Compaction has a reset hazard that rotation does not: between rewriting the file and resetting the cursor, the two objects describe different logs. A reset in that window leaves a cursor pointing into a file whose contents shifted, and replay resumes at the wrong byte. Writing the compacted log to a temporary path and renaming it after the cursor is reset to 0 narrows the window to the rename itself: a reset before the rename leaves a cursor of 0 against the *old*, still-complete log, which replays everything the consumer must already deduplicate. The scheme depends on the rename replacing the old file in one commit rather than as a delete followed by a create, so firmware that relies on it should verify that behaviour on its own LittleFS version rather than assume it.

## Overflow policy

When an outage outlasts the flash budget, something is discarded, and the choice belongs in the code rather than in whatever the allocator happens to do. Discarding the **oldest** segment preserves the recent picture, which for air-quality trend monitoring is the more useful half; discarding the newest preserves the start of the incident. Either is defensible. The outcome to exclude is silent overflow, in which the gap is neither recorded nor visible: emitting a counter of dropped records lets the consumer distinguish "no readings because nothing was sampled" from "no readings because the buffer overflowed".

## Verification

The design is exercised by two deliberate faults. Blocking the broker for ten minutes while sampling continues tests the accumulating state, and reconnection should fill the gap from `queue.ndjson` with no missing timestamps. Removing power during replay tests the cursor: the node must resume from the saved offset and re-send at most the records after it, rather than restarting the log from the beginning.

## Pitfalls

- `format_if_mount_failed = true` erases the backlog on any mount error, not only on first boot; a partition that fails to mount because of a transient fault comes back empty and the buffered readings are gone.
- Advancing the cursor before the broker acknowledges converts at-least-once into at-most-once: a reset between the two writes drops the record permanently, and nothing downstream reports the loss.
- A record longer than the `fgets` buffer is split mid-line and published as two invalid JavaScript Object Notation fragments; the consumer sees parse errors rather than a truncation warning.
- `esp_mqtt_client_publish` returns as soon as the message is queued, so a cursor advanced on its return value records readings the broker may never have acknowledged; the `PUBACK` is reported separately as `MQTT_EVENT_PUBLISHED`.
- QoS 1 acknowledges broker receipt only. A broker that loses the message before delivering it leaves the cursor advanced past a record no subscriber ever saw.
- Compacting the log and resetting the cursor as two separate writes leaves a window in which a reset makes the cursor point into shifted data, and replay resumes mid-record.
- Without an idempotency key on the consumer, the duplicate records that at-least-once delivery guarantees appear as spurious repeated measurements in the time series.
- Silent overflow is indistinguishable from a sensor outage after the fact; without a dropped-record counter, a gap in the data has no attributable cause.
