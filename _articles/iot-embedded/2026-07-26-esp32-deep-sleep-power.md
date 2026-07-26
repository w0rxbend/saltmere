---
title: "Making an ESP32 air-quality node live for months on a LiPo"
date: 2026-07-26
track: iot-embedded
summary: "Deep sleep, RTC memory, and a duty-cycle math worksheet for turning a wake-measure-publish sensor loop into a battery budget you can actually trust."
reading_time: 5
tags: [esp32, deep-sleep, power-management, battery, low-power, rtc-memory]
sources:
  - title: "Sleep Modes — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/sleep_modes.html"
  - title: "ESP32-WROOM-32 Datasheet (Espressif Systems)"
    url: "https://www.mouser.com/datasheet/2/813/esp32-wroom-32_datasheet_en-1534385.pdf"
  - title: "Insight Into ESP32 Sleep Modes & Their Power Consumption — Last Minute Engineers"
    url: "https://lastminuteengineers.com/esp32-sleep-modes-power-consumption/"
  - title: "ESP32 Deep Sleep Current: What the Datasheet Says vs What You'll Actually Measure — Hubble Network"
    url: "https://hubble.com/community/guides/esp32-deep-sleep-current-what-the-datasheet-says-vs-what-you-ll-actually-measure/"
  - title: "ESP32 Power Consumption & Sleep Modes [All Variants] — DeepBlueMbedded"
    url: "https://deepbluembedded.com/esp32-sleep-modes-power-consumption/"
---

The SEN5x/MQTT node from the earlier article assumed mains power. Put the same sensor on a LiPo pouch cell and the entire firmware design flips: the question stops being "how do I publish a reading" and becomes "how many microamps am I burning while I'm *not* publishing a reading." For a periodic air-quality sensor — wake, sample, publish, sleep, repeat — that idle current dominates the battery math almost completely.

## The four power states

The ESP32 datasheet defines four operating modes, each trading away a different subsystem for current:

| Mode | What stays powered | Typical current | Wake latency |
|---|---|---|---|
| Active (Wi-Fi/BT RF) | Everything | 95–380 mA (bursty, TX-dependent) | — |
| Modem-sleep | CPU + RAM, RF duty-cycled | 20–68 mA (scales with CPU clock) | ~ms |
| Light-sleep | RAM + RTC domain, CPU/peripherals clock-gated | ~0.8 mA | <1 ms, state preserved |
| Deep-sleep | RTC controller, RTC memory, optionally ULP | 10 µA (timer-only) up to 150 µA (ULP active) | 200–500 ms (full reboot) |
| Hibernation | RTC timer only | ~5 µA | full reboot, RTC memory not guaranteed |

Figures are from Espressif's own ESP32-WROOM-32 datasheet and hold for the bare module — they assume nothing else on the board is drawing current. That caveat matters more than it sounds: a stock dev board's USB-serial bridge, power LED, and AMS1117 linear regulator can add several *milliamps* of constant leakage, swamping a 10 µA deep-sleep budget by three orders of magnitude. If you're designing for battery life, either strip a dev board down (desolder the LED, bypass the onboard LDO) or design your own board around a bare ESP32 module and a low-quiescent regulator.

**Modem-sleep** keeps the CPU running and RAM intact; it's what the Wi-Fi stack uses automatically between DTIM beacon intervals to stay associated without listening constantly — useful if you need to stay reachable, not useful for a node that only needs to phone home every few minutes.

**Light-sleep** clock-gates the CPU and most peripherals but keeps RAM powered, so execution resumes exactly where it left off. It's the right tool when you need sub-millisecond wake latency and don't want to lose call-stack state — an ADC sampling loop with short gaps between reads, for example.

**Deep-sleep** is the one that matters for a periodic sensor: it powers off the CPU, most of RAM, and every APB-clocked peripheral. On wake, the chip runs the full boot ROM and startup sequence again — there's no "resume," only "restart with some memory intact."

**Hibernation** goes one step further and powers down the ULP coprocessor and RTC memory too, leaving only the RTC timer alive. It's the lowest-power option but it means you lose RTC_SLOW/RTC_FAST memory contents — not useful if you need a persistent boot counter or cached calibration data.

## Wake sources

Deep-sleep and light-sleep both wake on one or more configured sources:

- **Timer** — `esp_sleep_enable_timer_wakeup(uint64_t time_in_us)`. The obvious choice for "sample every N minutes."
- **ext0** — `esp_sleep_enable_ext0_wakeup(gpio_num_t gpio_num, int level)`, a single RTC GPIO, level-triggered. Good for a single interrupt line (a PIR sensor, a button).
- **ext1** — `esp_sleep_enable_ext1_wakeup_io(uint64_t io_mask, esp_sleep_ext1_wakeup_mode_t mode)`, a bitmask of multiple RTC GPIOs, useful when several sensors can independently trigger a wake.
- **Touch pads** — `esp_sleep_enable_touchpad_wakeup(void)`, for capacitive-touch wake.
- **ULP coprocessor** — `esp_sleep_enable_ulp_wakeup(void)`. The ULP can poll a sensor over I2C/ADC while the main CPU stays fully off, only waking the CPU when a threshold is crossed — this is how you build a "wake only if PM2.5 spikes" node without burning main-CPU current on every poll.

Multiple sources can be armed simultaneously; `esp_sleep_get_wakeup_cause()` after boot tells you which one fired.

## What survives, what doesn't

Main SRAM is powered off in deep-sleep, so ordinary globals and the call stack are gone — every deep-sleep wake is a cold boot of `app_main()`/`setup()`. What survives is the RTC memory domain: 8 KB of RTC_SLOW memory plus RTC_FAST memory, which stay powered whenever anything is tagged with the `RTC_DATA_ATTR` attribute. Use it for a boot counter, a rolling calibration offset, or the last-known sensor reading you want to compare against on the next wake — anything you need across the sleep boundary that doesn't justify a flash write (which costs both wear and time).

## The wake-measure-sleep loop

```c
#include <stdio.h>
#include "esp_sleep.h"
#include "esp_system.h"

#define uS_TO_S_FACTOR 1000000ULL
#define SAMPLE_INTERVAL_S 600   // 10 minutes

// Survives deep sleep: lives in RTC_SLOW memory, zero-initialized only
// on power-on-reset, not on every deep-sleep wake.
RTC_DATA_ATTR static uint32_t boot_count = 0;
RTC_DATA_ATTR static float last_pm25 = -1.0f;

void app_main(void) {
    boot_count++;
    printf("Wake #%u, cause=%d, previous PM2.5=%.1f\n",
           boot_count, esp_sleep_get_wakeup_cause(), last_pm25);

    // ... init I2C, read SEN5x, connect Wi-Fi, publish MQTT ...
    float pm25 = read_pm25_and_publish();   // your existing sensor/MQTT code
    last_pm25 = pm25;

    esp_sleep_enable_timer_wakeup(SAMPLE_INTERVAL_S * uS_TO_S_FACTOR);
    printf("Entering deep sleep for %ds\n", SAMPLE_INTERVAL_S);
    esp_deep_sleep_start();   // never returns
}
```

`esp_deep_sleep_start()` doesn't return — the next line of code that runs is the top of `app_main()` after the reboot, with `boot_count` and `last_pm25` intact because they're RTC-attributed. Everything else — Wi-Fi state, TLS session, heap contents — is gone and must be rebuilt from scratch, which is the dominant cost in the active-time budget below.

## Battery-life math, worked

Take the air-quality node: wake every 10 minutes, spend ~2.2 seconds active (sensor read + Wi-Fi connect/publish, dominated by the Wi-Fi association and TX burst), then deep-sleep the rest on timer wakeup only.

Assumptions:
- Active current during the 2.2 s window: 150 mA average (blends boot, sensor I2C read, and Wi-Fi connect+publish bursts)
- Deep-sleep current: 10 µA (0.01 mA), timer wakeup only, no ULP
- Cycle length: 600 s (10 minutes)
- Battery: 2000 mAh LiPo pouch cell, 3.7 V nominal, ignoring self-discharge and regulator losses

Average current over one cycle:

```
avg_mA = (t_active × I_active + t_sleep × I_sleep) / t_cycle
       = (2.2 s × 150 mA + 597.8 s × 0.01 mA) / 600 s
       = (330 + 5.978) / 600
       = 0.56 mA
```

Battery life:

```
hours = capacity_mAh / avg_mA = 2000 / 0.56 ≈ 3570 hours
days  = 3570 / 24 ≈ 149 days
months ≈ 4.9 months
```

Two levers dominate this number, in order of impact: **active duration** (Wi-Fi connect/TLS handshake is usually the biggest single cost — caching credentials and using a fast broker reconnect shaves this hard) and **sample interval** (doubling the interval to 20 minutes roughly halves average current and doubles runtime, since sleep current is already near-zero). Sleep current itself only matters once you've already gotten active time down — going from 10 µA to 100 µA barely moves the total here, but it dominates if you enable ULP sensor polling between wakes.

## Try it against your own board

The numbers above are datasheet-typical for the bare module; measure your actual board with a current-limited bench supply or an inline shunt before trusting a runtime estimate; dev-board leakage from USB bridges and status LEDs routinely adds low-single-digit milliamps that a 10 µA sleep budget can't hide from.

**Try next:** wire an ext1 wakeup on the SEN5x's fan-fault or interrupt pin alongside the timer wakeup, so the node samples on schedule but also wakes early on a hardware fault — then check `esp_sleep_get_wakeup_cause()` to branch firmware behavior per wake reason.
