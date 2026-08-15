---
title: "Deep sleep and duty-cycle budgeting for a battery-powered ESP32 sensor node"
date: 2026-07-26
track: iot-embedded
summary: "Deep sleep, RTC memory retention, and the duty-cycle arithmetic that turns a wake-measure-publish sensor loop into a defensible battery budget."
reading_time: 6
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

**Gist.** A periodic air-quality node running from a lithium-polymer (LiPo) cell spends almost all of its life idle, so idle current, not publish current, sets the runtime. Deep sleep removes that idle cost by powering down the central processing unit (CPU), main static RAM (SRAM) and the advanced peripheral bus (APB) clock domain, leaving only the real-time-clock (RTC) controller alive at roughly 10 µA. The cost is that **every wake is a cold boot**: no call stack, no heap, no Wi-Fi association and no Transport Layer Security (TLS) session survive, so each cycle pays the full reconnect time again.

The SEN5x/MQTT node from the earlier article assumed mains power. On a battery the design question changes from how a reading is published to how much current is drawn while no reading is being published.

## The power states

The ESP32 datasheet defines the following operating modes, each trading a subsystem for current:

| Mode | What stays powered | Typical current | Wake latency |
|---|---|---|---|
| Active (Wi-Fi/BT RF) | Everything | 95–240 mA (bursty, TX-dependent) | — |
| Modem-sleep | CPU + RAM, RF duty-cycled | 20–68 mA (scales with CPU clock) | ~ms |
| Light-sleep | RAM + RTC domain, CPU/peripherals clock-gated | ~0.8 mA | resumes in place, state preserved |
| Deep-sleep | RTC controller, RTC memory, optionally ULP | 10 µA (timer-only) up to 150 µA (ULP active) | full reboot |
| Hibernation | RTC timer only | ~5 µA | full reboot, RTC memory not guaranteed |

Figures come from Espressif's ESP32-WROOM-32 datasheet and describe **the bare module in isolation**; they assume no other component on the board draws current. That caveat is load-bearing. A stock development board's USB-to-serial bridge, power LED and AMS1117 linear regulator can add several *milliamps* of constant leakage, which exceeds a 10 µA sleep budget by two orders of magnitude or more. Battery-oriented designs therefore either strip the development board (remove the LED, bypass the onboard low-dropout regulator) or use a bare module with a low-quiescent-current regulator.

**Modem-sleep** keeps the CPU running and SRAM intact; the Wi-Fi stack uses it between delivery-traffic-indication-message (DTIM) beacons to remain associated without listening continuously. It suits a node that must stay reachable, not one that reports every few minutes.

**Light-sleep** clock-gates the CPU and most peripherals while keeping SRAM powered, so execution resumes at the instruction after the sleep call. It is the appropriate mode when a reboot cannot be afforded between gaps and call-stack state must survive — an analogue-to-digital-converter (ADC) sampling loop with short gaps, for instance.

**Deep-sleep** powers off the CPU, most of SRAM and every APB-clocked peripheral. On wake the chip re-runs the boot read-only memory (ROM) and the startup sequence. There is no resume path, only a restart with part of memory intact.

**Hibernation** additionally powers down the ultra-low-power (ULP) coprocessor and RTC memory, leaving the RTC timer alone. It draws the least current and loses RTC_SLOW and RTC_FAST memory contents, which rules it out where a persistent boot counter or cached calibration is required.

## Wake sources and the arming invariant

Deep-sleep and light-sleep wake on one or more configured sources:

- **Timer** — `esp_sleep_enable_timer_wakeup(uint64_t time_in_us)`, the periodic case.
- **ext0** — `esp_sleep_enable_ext0_wakeup(gpio_num_t gpio_num, int level)`: a single RTC-capable general-purpose input/output (GPIO), level-triggered.
- **ext1** — `esp_sleep_enable_ext1_wakeup_io(uint64_t io_mask, esp_sleep_ext1_wakeup_mode_t mode)`: a bitmask over several RTC GPIOs, for multiple independent trigger lines.
- **Touch pads** — `esp_sleep_enable_touchpad_wakeup(void)`.
- **ULP coprocessor** — `esp_sleep_enable_ulp_wakeup(void)`. The ULP polls a sensor over the inter-integrated-circuit (I2C) bus or the ADC while the main CPU remains off, raising a wake only when a threshold is crossed.

The invariant that governs correctness here: **a source that is not armed before `esp_deep_sleep_start()` cannot wake the chip**, and since the wake path restarts `app_main()` from the top, arming must be re-executed on every cycle rather than once at first boot. Multiple sources may be armed simultaneously; `esp_sleep_get_wakeup_cause()` after boot reports which one fired.

## What survives the sleep boundary

Main SRAM is unpowered in deep-sleep, so ordinary globals and the call stack are lost — every deep-sleep wake is a cold entry into `app_main()`. The surviving domain is RTC memory: **8 KB of RTC_SLOW memory plus RTC_FAST memory**, retained for objects tagged with the `RTC_DATA_ATTR` attribute. Such objects are zero-initialised on power-on reset but not on a deep-sleep wake, which makes them suitable for a boot counter, a rolling calibration offset, or the previous reading used for comparison — state that must cross the boundary but does not justify a flash write, which costs both wear and time.

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
    float pm25 = read_pm25_and_publish();
    last_pm25 = pm25;

    // Re-armed on every wake: the previous arming did not survive the reboot.
    esp_sleep_enable_timer_wakeup(SAMPLE_INTERVAL_S * uS_TO_S_FACTOR);
    printf("Entering deep sleep for %ds\n", SAMPLE_INTERVAL_S);
    esp_deep_sleep_start();   // never returns
}
```

`esp_deep_sleep_start()` does not return. The next instruction executed is the top of `app_main()` after the reboot, with `boot_count` and `last_pm25` intact because they are RTC-attributed. Wi-Fi state, TLS session and heap contents are gone and must be rebuilt, which is the dominant term in the active-time budget below.

## Battery-life arithmetic

Take the air-quality node: wake every 10 minutes, remain active for about 2.2 s (sensor read plus Wi-Fi association and publish), then deep-sleep on timer wakeup only.

Assumptions:
- Active current during the 2.2 s window: 150 mA average, blending boot, the I2C sensor read, and the Wi-Fi connect and transmit bursts
- Deep-sleep current: 10 µA (0.01 mA), timer wakeup only, no ULP
- Cycle length: 600 s
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

The structure of the expression explains which levers matter. The active term contributes 330 mA·s per cycle against 5.978 mA·s from sleep, a ratio of roughly 55:1, so **active duration dominates** — the Wi-Fi association and TLS handshake are typically its largest component. **Sample interval** is the second lever: doubling it to 20 minutes roughly halves average current, because the sleep term is small enough that stretching it adds little. Sleep current itself only becomes significant once active time has been reduced; raising it from 10 µA to 100 µA moves the average from 0.56 mA to roughly 0.65 mA, a change of about a sixth, against the halving available from doubling the interval. It becomes the leading term only once active time is short or the ULP polls a sensor between wakes.

## Pitfalls

- **Datasheet sleep current measured on an unmodified development board.** Symptom: measured idle current of several milliamps against a 10 µA budget, and a runtime an order of magnitude short of the estimate. Cause: the USB-to-serial bridge, power LED and linear regulator draw current independently of the module's sleep state.
- **Wake source armed only on first boot.** Symptom: the node sleeps once and never wakes. Cause: deep-sleep wake restarts `app_main()`, so any arming call placed outside the per-cycle path is not re-executed.
- **State held in ordinary globals across sleep.** Symptom: a boot counter reads 1 on every wake and comparisons against the previous sample never fire. Cause: main SRAM is unpowered in deep-sleep; only `RTC_DATA_ATTR` objects are retained.
- **Hibernation chosen for its lower current while relying on retained state.** Symptom: the boot counter and cached calibration are lost. Cause: hibernation powers down RTC memory, whose contents are not guaranteed across the transition.
- **Optimising sleep current before active duration.** Symptom: substantial effort on leakage yields a negligible runtime change. Cause: with a 2.2 s active window at 150 mA against 600 s at 10 µA, the active term is roughly 55 times the sleep term.
- **Treating an ext1 wake as a timer wake.** Symptom: a node that woke early on a hardware fault publishes as though the interval had elapsed. Cause: the branch on `esp_sleep_get_wakeup_cause()` is absent, and both sources enter the same code path.
