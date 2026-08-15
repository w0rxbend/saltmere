---
title: "Keeping Time on ESP32 Sensor Nodes: SNTP, Deep Sleep, and Clocks That Lie"
date: 2026-08-15
track: iot-embedded
summary: "Store-and-forward sensor data is worthless without trustworthy timestamps. The esp_netif_sntp API in ESP-IDF 5+, smooth vs immediate adjustment, how badly the internal 150 kHz RC oscillator drifts through deep sleep versus a 32 kHz crystal, and a sync-once-a-day pattern that timestamps at capture."
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

The [store-and-forward article](/articles/iot-embedded/2026-07-31-littlefs-store-and-forward/) buffers readings in LittleFS while Wi-Fi is down and replays them later. That design has a silent dependency: every buffered record needs a timestamp that was *correct when the reading was taken*, hours before upload. Get that wrong and your influx of backfilled data lands as a spike at upload time, or — worse — at plausible-looking wrong times nobody ever questions. Timekeeping on a battery node that deep-sleeps 99% of its life is its own small discipline.

## What survives sleep, and what it counts with

ESP-IDF keeps system time with two clocks: a high-resolution timer while the chip runs, and the **RTC timer**, which keeps counting through deep sleep and survives any reset except power-on. So `time(NULL)` after wake is continuous — no re-sync needed per wake. The question is what the RTC timer counts *with*. Four options via `CONFIG_RTC_CLK_SRC`:

- **Internal ~150 kHz RC oscillator** (the default, spec'd 90–150 kHz): zero external parts, lowest deep-sleep current — and, per Espressif's own docs, its frequency moves with temperature, so "time may drift in both Deep-sleep and Light-sleep modes."
- **External 32.768 kHz crystal**: proper watch-crystal stability for ~1 µA extra deep-sleep current. Needs the crystal on GPIO32/33 (boards like the FireBeetle have the pads; most dev boards don't).
- **External 32 kHz oscillator** feeding a clock in.
- **Internal 8.5–17.5 MHz oscillator ÷ 256**: better than the RC for ~5 µA more, still no external parts.

How bad is the default? The RC oscillator is calibrated against the main 40 MHz crystal at boot, but between calibrations the drift is on the order of *minutes per day* — a classic ESP32 forum thread measured 2 minutes fast over 10 hours, roughly 3,000 ppm. A real 32 kHz crystal is a ±10–20 ppm part: a second or two per day. If your node sleeps in 10-minute cycles and syncs daily, the RC clock's error stays bounded to tens of seconds per cycle window — acceptable for air-quality trends, not for anything that correlates events across nodes. (If you need milliseconds, that's a different sport: disciplined NTP with regression over the offset history, as the Lectrobox project demonstrates to ~200 µs median.)

## esp_netif_sntp: the IDF 5+ way

Old code called `sntp_init()` from LwIP directly; ESP-IDF 5.1+ wraps it in a thread-safe API that also composes with the blocking startup flow a sleepy node wants:

```c
#include "esp_netif_sntp.h"

esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
cfg.smooth_sync = false;                      // step the clock (see below)
esp_netif_sntp_init(&cfg);
if (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(10000)) != ESP_OK) {
    // no server reachable — carry on with RTC time
}
esp_netif_sntp_deinit();
```

If the node stays awake, LwIP re-syncs on its own every `CONFIG_LWIP_SNTP_UPDATE_DELAY` (default one hour). A deep-sleeping node never lives that long, which is why the explicit init/wait/deinit dance around your upload window is the right shape.

**Smooth vs immediate.** The default mode steps the clock with `settimeofday()` the moment a response arrives — time can jump backwards, and a naive `readings[i].ts > readings[i-1].ts` invariant breaks. `SNTP_SYNC_MODE_SMOOTH` instead slews via `adjtime()`, keeping time monotonic, and falls back to an immediate step only when the error exceeds 35 minutes. For an always-on gateway, smooth is strictly nicer. For a node awake 8 seconds a day, smooth is useless — `adjtime` slews far slower than your awake window — so step immediately, and do it *before* stamping anything new.

## Timezones without pain

Store and transmit **UTC epoch seconds, always**. Timezone is a display concern; apply it only where a human reads a clock, via the POSIX `TZ` machinery:

```c
setenv("TZ", "CET-1CEST,M3.5.0,M10.5.0/3", 1);   // Central Europe w/ DST rules
tzset();
// localtime() now does DST correctly; time(NULL) stays UTC
```

Bake the rule string into firmware once; nodes that log local time into records inevitably double-convert somewhere downstream.

## The daily-sync pattern

Everything above composes into a small policy: timestamp **at capture**, sync **once a day**, and mark records taken before first-ever sync as untrusted.

```c
#include "esp_sleep.h"
#include "esp_netif_sntp.h"

#define SYNC_INTERVAL  (24 * 3600)
#define TIME_VALID(t)  ((t) > 1755000000)        // sanity: after mid-2026

RTC_DATA_ATTR static time_t last_sync;           // survives deep sleep

void app_main(void) {
    time_t now = time(NULL);

    bool need_sync = !TIME_VALID(now) || (now - last_sync) > SYNC_INTERVAL;
    if (need_sync) {
        wifi_connect();                          // your STA bring-up
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

Details that earn their keep: `last_sync` lives in `RTC_DATA_ATTR` memory, so the daily schedule survives sleep but not a power cycle — exactly right, since power-on also resets the RTC timer and forces a fresh sync anyway. The validity check catches the first boots before any sync, when the clock reads 1970; flagging those records beats inventing times for them. And stamping happens after the potential step so a sync never re-dates a reading. Timestamping at capture rather than at upload is the entire point — with drift bounded by daily syncs, a record uploaded six hours late still says when the air was actually measured.

When to resync more often: after OTA (reboot keeps RTC time, so cheap), whenever a computed drift-per-day estimate (compare pre-sync time against server time, store the delta) exceeds your tolerance, and any time you see the clock invalid. Logging that pre-sync delta is free and tells you empirically which oscillator your fleet is really living on.

**Try next:** log the correction SNTP applies at each daily sync for a week — that's your node's real drift rate in seconds/day; if it's over a minute, that's the case for soldering a 32 kHz crystal onto the next board spin.
