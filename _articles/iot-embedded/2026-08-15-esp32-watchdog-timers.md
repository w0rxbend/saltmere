---
title: "ESP32 Watchdogs: Interrupt WDT, Task WDT, and RTC WDT"
date: 2026-08-15
track: iot-embedded
summary: "The ESP32 carries three watchdogs — the interrupt watchdog, the task watchdog, and the RTC watchdog — and each covers a different failure class. This article describes what trips them (blocking loops, long interrupt service routines, flash writes starving the idle task), the ESP-IDF 5.x esp_task_wdt configuration-struct application programming interface, and the feeding discipline that makes a watchdog diagnostic rather than ornamental."
reading_time: 6
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

**Gist.** A firmware node that hangs silently is harder to diagnose than one that crashes: a crash leaves a reset reason and a backtrace, a hang leaves only a gap in telemetry. The ESP32 provides three independent watchdog timers — one over interrupt processing, one over scheduled tasks, one over the boot path — that convert a hang into a recorded panic or a hard reset. The cost is that every watched code path must periodically prove liveness, so worst-case latencies (flash erases, long critical sections, blocking network calls) have to be budgeted into the timeout rather than discovered by spurious resets.

## Three watchdogs, three failure classes

The **interrupt watchdog (IWDT)** is driven by a hardware watchdog timer and fires when *interrupt processing itself* is blocked — specifically when the FreeRTOS tick interrupt service routine (ISR) has not run for `CONFIG_ESP_INT_WDT_TIMEOUT_MS`, whose default is **300 ms** (larger when external pseudo-static RAM, PSRAM, is enabled). The conditions that produce this are interrupts disabled for too long, an over-long critical section entered with `portENTER_CRITICAL`, or an ISR that does not return. The IWDT **always panics**, printing `Interrupt wdt timeout on CPU0` and naming the central processing unit (CPU) core involved; if the panic handler itself is wedged, a second-stage timeout hard-resets the chip.

The **task watchdog (TWDT)** is a software watchdog over *subscribed tasks*. Each subscribed task must call `esp_task_wdt_reset()` within `CONFIG_ESP_TASK_WDT_TIMEOUT_S`, default **5 s**. The load-bearing detail is that **the idle tasks are subscribed by default**, which extends coverage from tasks that opted in to any task that monopolises a core: a task that never yields starves that core's idle task, and the idle task is what misses the deadline. By default a TWDT timeout prints a warning and a backtrace and execution continues; a reset requires `CONFIG_ESP_TASK_WDT_PANIC` or the `trigger_panic` field below.

The **RTC watchdog (RTC_WDT)**, driven from the real-time clock (RTC) domain, covers the interval the other two cannot: from power-on through the bootloader until `app_main`. A corrupted application image or a flash fault that hangs boot produces a hard reset rather than an indefinitely dead device. It is disabled once user code starts unless `CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE` keeps it running.

| | IWDT | TWDT | RTC WDT |
|---|---|---|---|
| Watches | ISRs / tick interrupt | Subscribed tasks + idle tasks | Boot process |
| Default timeout | 300 ms | 5 s | bootloader-managed |
| On timeout | Panic (always) | Warning, or panic if configured | Hard reset |
| Typical culprit | Long ISR, critical section | Blocking loop, starved idle task | Corrupt image, brownout |

## The ESP-IDF 5.x interface

ESP-IDF 5.x replaced the two-argument `esp_task_wdt_init(timeout, panic)` with a **configuration struct**. When `CONFIG_ESP_TASK_WDT_INIT` is enabled — the default — the TWDT is already running by the time `app_main` executes, and reconfiguration goes through `esp_task_wdt_reconfigure()` rather than a fresh init.

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
        .idle_core_mask = (1 << 0) | (1 << 1),    /* watch idle task on both cores */
        .trigger_panic  = true,                   /* panic handler, not just a log line */
    };
    ESP_ERROR_CHECK(esp_task_wdt_reconfigure(&cfg));

    xTaskCreate(sensor_task, "sensor", 4096, NULL, 5, NULL);
}
```

Bit *n* of `idle_core_mask` subscribes core *n*'s idle task; the same subscriptions are settable at build time through `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU0` and `..._CPU1`. `esp_task_wdt_add(NULL)` subscribes the calling task. **A subscribed task must call `esp_task_wdt_delete()` before it exits**, because the subscription outlives the task and the now-absent task can never feed again.

## Conditions that trip them in deployed firmware

- **A loop without `vTaskDelay` or any other blocking call.** A task at priority 5 spinning on a core prevents the idle task from running there, so the *idle* task misses its feed and the report names `IDLE0` rather than the offending task. The guilty task appears in the printed list of currently running tasks, not in the name of the task that timed out.
- **Long ISRs.** Formatted printing, inter-integrated-circuit (I2C) transactions, or heavy arithmetic inside an ISR instead of deferral to a task through a queue or a task notification. Once interrupts are blocked past the IWDT timeout, the panic is unconditional.
- **Flash operations starving everything else.** During non-volatile storage (NVS) commits and over-the-air (OTA) update writes the flash cache is disabled, so any code not resident in instruction RAM (IRAM) stalls until the operation finishes. A large OTA write combined with non-IRAM ISRs is a common IWDT trigger, and long erase operations can exceed a tight TWDT timeout. **The timeout must be budgeted against the worst-case flash operation, not the average loop iteration.**
- **Blocking network calls.** A domain name system (DNS) lookup or a `connect()` issued without a timeout on an unreachable network can block for tens of seconds, which exceeds the 5 s TWDT default by a wide margin.

## Converting a timeout into evidence

A watchdog reset that cannot be attributed to a code path replaces one unknown with another. With `trigger_panic` set and core dumps to flash enabled (`CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH`), the panic handler writes task state to the core-dump partition before rebooting. The dump is retrievable afterwards with `idf.py coredump-info`, which yields the backtrace of the task that failed to feed the watchdog — available long after the event and without a serial cable attached at the time of failure. `esp_reset_reason()` at boot distinguishes the cause (`ESP_RST_TASK_WDT`, `ESP_RST_INT_WDT`, `ESP_RST_WDT`); publishing that value in telemetry makes reboot loops visible remotely instead of appearing as an unexplained gap.

The invariant that makes the mechanism useful: **the feed must be conditional on observed progress, not on reaching a line of code.** A feed issued unconditionally at the top of the loop, or from a periodic timer callback, proves only that the scheduler still dispatches the task — a task blocked forever on a wedged I2C bus can keep such a watchdog satisfied indefinitely. Feeding once per iteration *after* verifying that the iteration accomplished its work (sensor read succeeded, queue drained, publish acknowledged), as in the snippet above, makes a stalled peripheral produce a reset.

## Pitfalls

- The TWDT report names `IDLE0` or `IDLE1`, not the task at fault; the offending task is identified only in the accompanying list of currently running tasks, so reading the timeout name alone misdirects the search.
- A TWDT timeout produces only a warning by default, so a device can log timeouts continuously without ever resetting or generating a core dump; the panic path requires `CONFIG_ESP_TASK_WDT_PANIC` or `trigger_panic`.
- A task that exits without calling `esp_task_wdt_delete()` leaves its subscription in place, and the TWDT then times out on a task that no longer exists.
- Calling `esp_task_wdt_init()` when `CONFIG_ESP_TASK_WDT_INIT` already started the watchdog is the wrong entry point in ESP-IDF 5.x; reconfiguration goes through `esp_task_wdt_reconfigure()`.
- Placing an ISR handler in flash rather than IRAM makes it unrunnable while the flash cache is disabled during NVS or OTA writes, so the stall shows up as an IWDT panic during an update rather than as a flash error.
- The RTC watchdog stops guarding once user code starts unless `CONFIG_BOOTLOADER_WDT_DISABLE_IN_USER_CODE` is configured, so a hang in early application initialisation falls to the task watchdog alone, which by default only warns.
- Enabling PSRAM lengthens the default interrupt-watchdog timeout, so timing behaviour measured on a PSRAM-equipped module does not transfer to a module without it.
