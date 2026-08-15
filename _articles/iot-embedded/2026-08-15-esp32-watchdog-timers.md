---
title: "ESP32 Watchdogs: Interrupt WDT, Task WDT, and Surviving Your Own Bugs"
date: 2026-08-15
track: iot-embedded
summary: "The ESP32 ships with three watchdogs — the interrupt WDT, the task WDT, and the RTC WDT — and each one catches a different class of bug you will eventually write. Here's what actually trips them (blocking loops, long ISRs, flash writes starving IDLE), the ESP-IDF 5.x esp_task_wdt config-struct API, and the design pattern that makes a watchdog useful instead of ornamental: feed it only when your loop provably made progress."
reading_time: 5
tags: [esp32, esp-idf, watchdog, freertos, reliability, debugging]
sources:
  - title: "Watchdogs — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/wdts.html"
  - title: "esp_task_wdt.h — espressif/esp-idf"
    url: "https://github.com/espressif/esp-idf/blob/master/components/esp_system/include/esp_task_wdt.h"
  - title: "Task Watchdog example — espressif/esp-idf"
    url: "https://github.com/espressif/esp-idf/blob/master/examples/system/task_watchdog/main/task_watchdog_example_main.c"
  - title: "Fatal Errors (panic handler) — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/fatal-errors.html"
  - title: "Core Dump — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/core_dump.html"
---

A sensor node that hangs in a closet for three weeks is worse than one that crashes and reboots in four seconds. Crashes leave evidence; hangs leave a gap in your Grafana dashboard and a ladder trip. The ESP32 gives you three separate watchdogs to convert hangs into evidence, and they catch three different classes of bug — worth knowing which one just fired before you go hunting.

## Three watchdogs, three failure classes

The **Interrupt Watchdog (IWDT)** runs off a hardware timer per CPU and fires when *interrupt processing itself* is blocked — the FreeRTOS tick ISR hasn't run for `CONFIG_ESP_INT_WDT_TIMEOUT_MS` (default 300 ms, longer with PSRAM). That means someone disabled interrupts too long, sat in a critical section (`portENTER_CRITICAL`), or wrote an ISR that never returns. It always panics: `Interrupt wdt timeout on CPU0`, and if the panic handler itself is wedged, a second-stage timeout hard-resets the chip.

The **Task Watchdog (TWDT)** is softer: a timer-service watchdog that monitors *subscribed tasks*. Each subscribed task must call `esp_task_wdt_reset()` within the timeout (default 5 s via `CONFIG_ESP_TASK_WDT_TIMEOUT_S`). Crucially, the **idle tasks** are usually subscribed — so any task that hogs a core without ever yielding to IDLE trips it. By default a TWDT timeout only prints a warning and backtrace and keeps running; set `CONFIG_ESP_TASK_WDT_PANIC` (or `trigger_panic` below) if you want a reset.

The **RTC Watchdog (RTC_WDT)** guards the window you can't: from power-on through the bootloader until `app_main`. A corrupted app or a flash glitch that hangs boot gets a hard reset instead of a brick. It's disabled once user code starts unless you keep it alive with `CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE`.

| | IWDT | TWDT | RTC WDT |
|---|---|---|---|
| Watches | ISRs / tick interrupt | Subscribed tasks + IDLE | Boot process |
| Default timeout | 300 ms | 5 s | bootloader-managed |
| On timeout | Panic (always) | Warning, or panic if configured | Hard reset |
| Typical culprit | Long ISR, critical section | Blocking loop, starved IDLE | Corrupt image, brownout |

## The ESP-IDF 5.x API

ESP-IDF 5.x replaced the old two-argument `esp_task_wdt_init(timeout, panic)` with a **config struct**. If `CONFIG_ESP_TASK_WDT_INIT` is enabled (the default), the TWDT is already running when `app_main` starts and you call `esp_task_wdt_reconfigure()` instead:

```c
#include "esp_task_wdt.h"

static void sensor_task(void *arg)
{
    ESP_ERROR_CHECK(esp_task_wdt_add(NULL));      /* subscribe THIS task */

    while (1) {
        bool sampled   = read_sensors();          /* each step reports success */
        bool published = publish_readings();

        if (sampled && published) {
            esp_task_wdt_reset();                 /* feed ONLY on proven progress */
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void app_main(void)
{
    esp_task_wdt_config_t cfg = {
        .timeout_ms     = 10000,
        .idle_core_mask = (1 << 0) | (1 << 1),    /* watch IDLE on both cores */
        .trigger_panic  = true,                   /* reset + core dump, not a log line */
    };
    ESP_ERROR_CHECK(esp_task_wdt_reconfigure(&cfg));

    xTaskCreate(sensor_task, "sensor", 4096, NULL, 5, NULL);
}
```

The `idle_core_mask` bits subscribe each core's idle task (also settable via `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0/1`). `esp_task_wdt_add(NULL)` subscribes the calling task; `esp_task_wdt_delete()` unsubscribes before a task exits — forgetting that is its own classic timeout.

## What actually trips them in real firmware

- **A loop with no `vTaskDelay`.** A task spinning at priority 5 never lets IDLE run on that core, so the *idle* task misses its feed and the TWDT prints `task_wdt: ... IDLE0` — the guilty task is in the printed list of "currently running" tasks, not the named one.
- **Long ISRs.** Doing `printf`, I2C transactions, or math in an ISR instead of deferring to a task via a queue or task notification. Past 300 ms of blocked interrupts, the IWDT panics.
- **Flash writes starving everything.** During NVS commits and OTA writes the flash cache is disabled; any code not in IRAM stalls. A big OTA chunk plus non-IRAM ISRs is a classic IWDT trigger, and long erases regularly push a tight TWDT over the line. Budget your timeout for your worst-case flash operation, not your average loop.
- **Blocking network calls** — a DNS lookup or `connect()` with no timeout on a dead LAN will happily sit for tens of seconds.

## Turn a timeout into evidence

A watchdog reset you can't diagnose just converts one mystery into another. Set `trigger_panic`, enable **core dumps to flash** (`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH`), and the panic handler writes every task's state to a coredump partition before rebooting. After the node comes back and reconnects, pull it with `idf.py coredump-info` and you get the backtrace of the task that starved the watchdog — weeks later, no serial cable attached. Check `esp_reset_reason()` at boot (`ESP_RST_TASK_WDT`, `ESP_RST_INT_WDT`, `ESP_RST_WDT`) and publish it in your telemetry so reboot loops are visible from the dashboard.

The design rule that makes all of this worthwhile: **never feed the watchdog from a timer or an unconditional line at the top of the loop.** That watchdog only proves the scheduler is alive. Feed it exactly once per iteration, *after* checking that the iteration achieved something — sensor read OK, queue drained, publish acknowledged — as in the snippet above. A node stuck reading a wedged I2C bus forever then reboots (and an I2C bus reset at startup often fixes exactly that), instead of reporting "alive" while doing nothing.

**Try next:** add `esp_reset_reason()` to your boot telemetry today, then deliberately write a `while(1);` into a task and confirm you can walk from the MQTT reboot notice to the flash core dump to the guilty line.
