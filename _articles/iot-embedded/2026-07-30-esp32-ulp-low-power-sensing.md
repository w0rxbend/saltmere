---
title: "Sampling a sensor while the ESP32 sleeps: the ULP coprocessor"
date: 2026-07-30
track: iot-embedded
summary: "Deep sleep saves current, but a sleeping ESP32 can't watch a sensor. The ULP coprocessor can — it runs a tiny program in RTC power domain while both main cores are off, samples an ADC or GPIO, and wakes the CPU only when a threshold trips. Here's the ULP-RISC-V/LP-core programming model and the µA math that justifies it."
reading_time: 5
tags: [esp32, ulp, low-power, deep-sleep, esp-idf, risc-v, coprocessor]
sources:
  - title: "ULP RISC-V Coprocessor Programming (ESP32-S3) — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/system/ulp-risc-v.html"
  - title: "ULP LP Core Coprocessor Programming (ESP32-C6) — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32c6/api-reference/system/ulp-lp-core.html"
  - title: "Building low power applications: the ULP LP core — Espressif Developer Portal"
    url: "https://developer.espressif.com/blog/2025/04/ulp-lp-core-get-started/"
  - title: "esp-idf/examples/system/ulp/ulp_riscv/gpio — Espressif (GitHub)"
    url: "https://github.com/espressif/esp-idf/tree/master/examples/system/ulp/ulp_riscv/gpio"
  - title: "Insight Into ESP32 Sleep Modes & Their Power Consumption — Last Minute Engineers"
    url: "https://lastminuteengineers.com/esp32-sleep-modes-power-consumption/"
---

The [deep-sleep article](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power) got a periodic node down to single-digit microamps by shutting off both main cores and waking on an RTC timer. That works when your schedule is *time*-driven — sample every five minutes. It falls apart when the schedule is *event*-driven: wake when the water level crosses a line, when a door opens, when a gas reading spikes. A sleeping CPU sees none of that, and waking every few seconds just to check burns the battery you were trying to protect.

The ULP coprocessor is the answer. It's a tiny processor that lives in the RTC power domain, which stays powered during deep sleep. It runs your program on a timer while the main cores are dark, reads RTC-connected peripherals, and pokes the CPU awake only when your condition is met. Duty-cycled polling at microamp cost.

## Three flavors, know which chip you have

There isn't one ULP — there are three, and the programming model differs:

| Coprocessor | Chips | Programmed in |
|---|---|---|
| FSM ULP | ESP32, ESP32-S2, ESP32-S3 | Assembly or C macros |
| ULP-RISC-V | ESP32-S2, ESP32-S3 | Standard C |
| LP core (RV32I) | ESP32-C6, ESP32-C5, ESP32-P4 | Standard C |

The original **FSM ULP** is a quirky finite-state-machine core with a handful of instructions; you write it in a special assembly or fiddly C-macro DSL. It's capable but painful. On the S2/S3 you can skip it entirely and use the **ULP-RISC-V** core, which runs ordinary C compiled by the RISC-V toolchain. The newest parts (C6, C5, P4) ship an **LP core** — a fuller RV32IMAC processor that can reach more peripherals and even stay running as a low-power companion while the main CPU is active. The ULP-RISC-V and LP-core APIs are nearly identical in shape (`ulp_riscv_*` vs `ulp_lp_core_*`), so porting between them is mechanical. This article uses ULP-RISC-V for the sensor example and shows the LP-core variant at the end.

## The ULP program: sample and decide

A ULP-RISC-V app is a separate C file that gets cross-compiled and embedded as a binary blob in your main firmware. Here's one that reads an ADC channel and wakes the CPU only when the reading exceeds a threshold. Any global you declare here becomes visible to the main CPU.

```c
// ulp/main.c  — runs on the ULP-RISC-V core
#include "ulp_riscv.h"
#include "ulp_riscv_utils.h"
#include "ulp_riscv_adc_ulp_core.h"

#define ADC_UNIT      ADC_UNIT_1
#define ADC_CHANNEL   ADC_CHANNEL_6
#define THRESHOLD     2500          // raw counts

// Shared with the main CPU. Prefixed 'ulp_' in the generated header.
volatile uint32_t last_reading = 0;
volatile uint32_t wake_count   = 0;

int main(void)
{
    uint16_t raw = ulp_riscv_adc_read_channel(ADC_UNIT, ADC_CHANNEL);
    last_reading = raw;

    if (raw > THRESHOLD) {
        wake_count++;
        ulp_riscv_wakeup_main_processor();
    }
    // Return; the ULP halts and the RTC timer re-launches main() next cycle.
    return 0;
}
```

There's no `while(1)` loop and no sleep call inside the ULP. The main CPU configures a wake period; the RTC timer relaunches `main()` on each tick, the code runs to completion in microseconds, and the core halts until the next tick. State you want to keep — `wake_count`, `last_reading` — lives in globals, which persist in RTC memory across relaunches.

## The main-CPU side: load, run, sleep

The main firmware compiles the ULP program (via the `ulp_embed_binary` CMake helper), loads the blob into RTC memory, starts it, arms the ULP wakeup source, and drops into deep sleep. The build system auto-generates `ulp_main.h` exposing your ULP globals as `ulp_last_reading`, `ulp_wake_count`, etc.

```c
#include "esp_sleep.h"
#include "ulp_riscv.h"
#include "ulp_main.h"

extern const uint8_t ulp_bin_start[] asm("_binary_ulp_main_bin_start");
extern const uint8_t ulp_bin_end[]   asm("_binary_ulp_main_bin_end");

static void start_ulp(void)
{
    ESP_ERROR_CHECK(ulp_riscv_load_binary(
        ulp_bin_start, (ulp_bin_end - ulp_bin_start)));

    // Re-launch the ULP every 500 ms.
    ulp_set_wakeup_period(0, 500 * 1000);   // period in microseconds
    ESP_ERROR_CHECK(ulp_riscv_run());
}

void app_main(void)
{
    if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_ULP) {
        // The ULP tripped the threshold — read the shared values.
        printf("Woken by ULP: reading=%lu, count=%lu\n",
               (unsigned long)ulp_last_reading,
               (unsigned long)ulp_wake_count);
        // ... take the real measurement, publish, etc.
    } else {
        start_ulp();   // first boot: initialize ADC config + launch ULP
    }

    ESP_ERROR_CHECK(esp_sleep_enable_ulp_wakeup());
    esp_deep_sleep_start();
}
```

On the very first boot you also initialize the ADC for ULP use on the main side with `ulp_riscv_adc_init()` (an `ulp_riscv_adc_cfg_t` naming the unit, channel, and attenuation) before launching the ULP — the ULP core reads the channel but the calibration/config is set up by the CPU. Watching a **GPIO threshold** instead of an ADC is the same skeleton: in the ULP, `ulp_riscv_gpio_init(pin)` then poll `ulp_riscv_gpio_get_level(pin)`, calling `ulp_riscv_wakeup_main_processor()` on the edge you care about. Espressif's `system/ulp/ulp_riscv/gpio` example is exactly this.

## Does it actually save current?

Yes, decisively, when your event is rare. From the ESP32 datasheet figures: pure deep sleep with just the RTC timer and RTC memory sits around **6–10 µA**, while running the ULP to monitor a sensor at a ~1% duty cycle averages about **100 µA**. Against an active core — tens of milliamps, e.g. an ESP32-S3-WROOM-1 module draws roughly **23.9 mA active** versus **8.1 µA in plain deep sleep** — even 100 µA is three orders of magnitude cheaper than keeping the CPU awake to poll.

The LP-core parts do better still. Espressif's own LP-core walkthrough measures a C6 running a periodic LP-core task at about **20 µA during the short wake-ups and 10 µA between them** — genuinely microamp-class continuous sensing. The LP-core code is the same story with renamed calls:

```c
ulp_lp_core_cfg_t cfg = {
    .wakeup_source = ULP_LP_CORE_WAKEUP_SOURCE_LP_TIMER,
    .lp_timer_sleep_duration_us = 500000,   // 500 ms
};
ESP_ERROR_CHECK(ulp_lp_core_load_binary(ulp_bin_start,
    ulp_bin_end - ulp_bin_start));
ESP_ERROR_CHECK(ulp_lp_core_run(&cfg));
ESP_ERROR_CHECK(esp_sleep_enable_ulp_wakeup());
esp_deep_sleep_start();
```

The decision rule: if your trigger fires often, the ULP's ~100 µA overhead may not beat just waking the CPU on a fast timer. If your trigger is rare — a threshold crossed once an hour — the ULP lets you watch continuously for the price of a coin cell's self-discharge.

**Try next:** flash Espressif's `examples/system/ulp/ulp_riscv/adc` on an S3 (or `ulp_lp_core/lp_adc` on a C6), put a multimeter in series with the 3.3 V rail, and watch the average current while you sweep the sensor past your threshold — then compare it to the same node polling from the main CPU.
