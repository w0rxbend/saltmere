---
title: "Keeping Time on ESP32 Sensor Nodes: SNTP, Deep Sleep, and Clocks That Drift"
date: 2026-08-15
track: iot-embedded
summary: "Store-and-forward sensor data is worthless without trustworthy timestamps. The esp_netif_sntp API in ESP-IDF 5+, smooth versus immediate adjustment, how far the internal 150 kHz RC oscillator drifts through deep sleep compared with a 32 kHz crystal, and a sync-once-a-day pattern that timestamps at capture."
reading_time: 6
tags: [esp32, sntp, ntp, deep-sleep, rtc, esp-idf, timekeeping]
sources:
  - title: "System Time — ESP-IDF Programming Guide (RTC clock sources, SNTP, sync modes)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/system_time.html"
  - title: "esp-idf/examples/protocols/sntp — SNTP + deep sleep example"
    url: "https://github.com/espressif/esp-idf/tree/master/examples/protocols/sntp"
  - title: "RTC clock too fast by 2 Min in 10 hours? — ESP32 Forum"
    url: "https://esp32.com/viewtopic.php?t=5391"
  - title: "High Precision NTP Client for the ESP32 — Lectrobox"
    url: "https://www.lectrobox.com/projects/esp32-ntp/"
---

**Gist.** A battery-powered node that buffers readings locally and uploads them hours later must record *when the reading was taken*, not when it was transmitted, so the timestamp has to come from a clock that keeps running through deep sleep. The mechanism is the ESP32 real-time clock (RTC) timer, which counts across deep sleep and every reset except power-on, periodically corrected by the Simple Network Time Protocol (SNTP). The cost is drift between corrections — on the default internal resistor-capacitor (RC) oscillator that drift is measured in minutes per day, and closing it requires either a crystal or a radio wake-up that spends energy purely on knowing the time.

The [store-and-forward article](/articles/iot-embedded/2026-07-31-littlefs-store-and-forward/) buffers readings in LittleFS while Wi-Fi is down and replays them later. That design carries a silent dependency: each buffered record needs a timestamp that was correct at capture. If it is not, backfilled data lands as a spike at upload time, or at plausible-looking wrong times that no consumer questions.

## What survives sleep, and what it counts with

ESP-IDF maintains system time with two clocks: a high-resolution timer while the chip runs, and the **RTC timer, which keeps counting through deep sleep and survives any reset except power-on**. Consequently `time(NULL)` after wake is continuous, and no re-sync is required per wake cycle. The open question is what the RTC timer counts with. `CONFIG_RTC_CLK_SRC` offers four sources:

- **Internal RC oscillator**, nominally around 150 kHz and the default: no external parts, lowest deep-sleep current. Espressif's documentation states its frequency moves with temperature and that "time may drift in both Deep-sleep and Light-sleep modes."
- **External 32.768 kHz crystal**: watch-crystal stability at the cost of the extra part and slightly higher deep-sleep current. The crystal is fitted on GPIO32/33; some boards expose the pads, most development boards do not.
- **External 32 kHz oscillator** driving a clock input.
- **Internal fast RC oscillator divided by 256**: more stable than the slow RC source, with no external parts.

The magnitude of the default's error is the deciding number. The RC oscillator is calibrated against the main 40 MHz crystal at boot, but **between calibrations the drift is on the order of minutes per day** — one ESP32 forum report measures 2 minutes fast over 10 hours, approximately 3,000 parts per million (ppm). A typical 32.768 kHz watch crystal is a ±20 ppm-class part, corresponding to a second or two per day. At 3,000 ppm the accumulated error reaches roughly four minutes over a full day, so a node sleeping in 10-minute cycles with a daily sync carries an error measured in minutes by the end of the window: adequate for air-quality trends, inadequate for correlating events across nodes. Sub-millisecond accuracy is a different problem entirely; the Lectrobox project disciplines the clock with a fit over the offset history rather than accepting each SNTP response as-is.

## esp_netif_sntp in ESP-IDF 5+

Older firmware called LwIP's `sntp_init()` directly. ESP-IDF 5.0 introduced `esp_netif_sntp`, a wrapper API that also composes with the blocking startup sequence a sleepy node needs.

```c
#include "esp_netif_sntp.h"

esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
cfg.smooth_sync = false;                      // step the clock (see below)
esp_netif_sntp_init(&cfg);
if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000)) != ESP_OK) {
    // no server reachable — continue on RTC time
}
esp_netif_sntp_deinit();
```

A node that stays awake is re-synced by LwIP every `CONFIG_LWIP_SNTP_UPDATE_DELAY`, one hour by default. **A deep-sleeping node never remains awake long enough for that timer to fire**, which is why the explicit init/wait/deinit sequence around the upload window is the appropriate shape.

**Smooth versus immediate adjustment.** The default mode steps the clock with `settimeofday()` as soon as a response arrives; time can therefore move backwards, and an invariant such as `readings[i].ts > readings[i-1].ts` is violated. `SNTP_SYNC_MODE_SMOOTH` instead slews the clock via `adjtime()`, preserving monotonicity, and falls back to an immediate step only when the error exceeds 35 minutes. Smooth mode suits an always-on gateway. For a node awake a few seconds per day it is ineffective, because **`adjtime()` slews far more slowly than the awake window lasts** — the correction never completes before the next sleep. Such a node should step, and step *before* stamping anything new.

## Timezones

Store and transmit **coordinated universal time (UTC) epoch seconds**. Timezone is a presentation concern, applied only where a human reads a clock, through the POSIX `TZ` machinery:

```c
setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3", 1);   // Central Europe with DST rules
tzset();
// localtime() now applies DST correctly; time(NULL) stays UTC
```

The rule string is compiled into firmware once. Records that carry local time invite a second conversion somewhere downstream, and the two conversions are not distinguishable after the fact.

## The daily-sync pattern

The preceding constraints compose into one policy: timestamp **at capture**, sync **once per day**, and mark records taken before the first successful sync as untrusted.

```c
#include "esp_sleep.h"
#include "esp_netif_sntp.h"

#define SYNC_INTERVAL  (24 * 3600)
#define TIME_VALID(t)  ((t) > 1755000000)        // sanity: after mid-2025

RTC_DATA_ATTR static time_t last_sync;           // survives deep sleep

void app_main(void) {
    time_t now = time(NULL);

    bool need_sync = !TIME_VALID(now) || (now - last_sync) > SYNC_INTERVAL;
    if (need_sync) {
        wifi_connect();                          // station-mode bring-up
        esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
        esp_netif_sntp_init(&cfg);
        if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000)) == ESP_OK)
            last_sync = time(NULL);
        esp_netif_sntp_deinit();
    }

    now = time(NULL);                            // stamp AFTER any step
    float co2 = read_sensor();
    char rec[64];
    int flags = TIME_VALID(now) ? 0 : 1;         // 1 = clock never synced
    snprintf(rec, sizeof(rec), "%lld,%.0f,%d\n", (long long)now, co2, flags);
    buffer_append(rec);                          // LittleFS store-and-forward

    upload_if_connected();
    esp_deep_sleep(600 * 1000000ULL);            // 10 min
}
```

Three details carry the design. `last_sync` resides in `RTC_DATA_ATTR` memory, so the daily schedule survives deep sleep but not a power cycle — which is consistent, since **power-on also resets the RTC timer** and forces a fresh sync regardless. The validity check catches the boots preceding any sync, when the clock reads the 1970 epoch; flagging those records is preferable to assigning them invented times. And stamping occurs after the potential step, so a sync never re-dates a reading already taken in the same wake cycle. Timestamping at capture rather than at upload is the whole point: with drift bounded by daily syncs, a record uploaded six hours late still states when the air was measured.

Additional sync triggers worth wiring in: after an over-the-air (OTA) update, since the reboot preserves RTC time and the sync is inexpensive; whenever a measured drift-per-day estimate exceeds the application's tolerance; and whenever the clock reads invalid. That estimate costs nothing to collect — record the difference between the pre-sync local time and the server time at each daily sync — and it reports empirically which oscillator a given fleet is running on. Over a week it yields the node's real drift rate in seconds per day, which is the evidence for or against fitting a 32.768 kHz crystal on the next board revision.

## Pitfalls

- **Timestamping at upload rather than at capture.** Symptom: hours of buffered readings appear compressed into a single moment in the time-series database. Cause: the record is stamped when it is transmitted, so every record in a backlog receives the upload time.
- **Enabling `SNTP_SYNC_MODE_SMOOTH` on a deep-sleeping node.** Symptom: the clock never converges and drift accumulates across days despite successful syncs. Cause: `adjtime()` slews gradually, and the node re-enters deep sleep before the slew completes.
- **Stamping a reading before the SNTP step.** Symptom: one record per sync cycle is dated in the future or the past relative to its neighbours. Cause: `settimeofday()` moved the clock after the reading was captured but within the same wake.
- **Assuming the default RC oscillator is adequate for cross-node correlation.** Symptom: events recorded by two nodes cannot be ordered against one another. Cause: drift on the order of thousands of ppm on the internal oscillator, against tens of ppm for a crystal.
- **Trusting the clock on first boot.** Symptom: records dated in 1970 flow into the database and distort aggregates. Cause: power-on resets the RTC timer, and no sync has occurred yet.
- **Storing local time in records.** Symptom: timestamps shift by an hour at daylight-saving-time boundaries, or by a fixed offset everywhere. Cause: a timezone applied on the node and applied again by a downstream consumer.
- **Expecting `CONFIG_LWIP_SNTP_UPDATE_DELAY` to re-sync a sleepy node.** Symptom: the clock is never corrected although SNTP is configured. Cause: the default one-hour re-sync interval never elapses within an awake window of seconds.
