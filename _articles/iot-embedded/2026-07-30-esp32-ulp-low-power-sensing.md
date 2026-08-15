---
title: "Sampling a sensor while the ESP32 sleeps: the ULP coprocessor"
date: 2026-07-30
track: iot-embedded
summary: "Deep sleep saves current, but a sleeping ESP32 cannot watch a sensor. The ultra-low-power (ULP) coprocessor can: it runs a small program in the RTC power domain while both main cores are off, samples an ADC channel or GPIO, and wakes the CPU only when a threshold trips. This article covers the ULP-RISC-V and LP-core programming model and the microamp arithmetic that justifies it."
reading_time: 6
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

**Gist.** Deep sleep on an ESP32 shuts down both main cores, so a node cannot observe a sensor between wake-ups; an event-driven condition — a water level crossing a line, a door opening — is invisible until the next timer wake, and polling on a fast timer spends the current that deep sleep was meant to save. The ultra-low-power (ULP) coprocessor resides in the real-time-clock (RTC) power domain, which remains powered during deep sleep, and is relaunched by the RTC timer to sample RTC-connected peripherals and signal a wake-up only when a condition is met. The cost is a raised sleep floor — monitoring with the ULP at roughly a 1 % duty cycle averages roughly **100 µA** against the **single-digit-to-10 µA** of a bare RTC-timer sleep — so the arrangement pays only when the monitored event is rare.

The [deep-sleep article](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power) reached single-digit microamps for a node whose schedule is *time*-driven: sample every five minutes. The ULP addresses the *event*-driven case, where the sampling instant is determined by the physical world rather than by a clock.

## Three coprocessor variants

There is no single ULP. Three exist, and the programming model differs between them.

| Coprocessor | Chips | Programmed in |
|---|---|---|
| FSM ULP | ESP32, ESP32-S2, ESP32-S3 | Assembly or C macros |
| ULP-RISC-V | ESP32-S2, ESP32-S3 | Standard C |
| LP core | ESP32-C6, ESP32-C5, ESP32-P4 | Standard C |

The original **finite-state-machine (FSM) ULP** exposes a small instruction set and is programmed either in a dedicated assembly dialect or through a C-macro domain-specific language. On the S2 and S3 the **ULP-RISC-V** core is available instead and runs ordinary C compiled by the RISC-V toolchain. The C6, C5 and P4 carry an **LP (low-power) core**, a RISC-V processor that reaches more peripherals and can also run while the main CPU is active, as a low-power companion rather than only as a sleep-time monitor. The two C-programmable APIs differ mainly by prefix (`ulp_riscv_*` against `ulp_lp_core_*`), so a port between them is largely mechanical. The example below uses ULP-RISC-V, with the LP-core form given afterwards.

## The ULP program: sample, compare, signal

A ULP-RISC-V application is a separate C translation unit, cross-compiled and embedded as a binary blob inside the main firmware image. Every global declared in it is visible to the main CPU through a generated header.

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
    int32_t raw = ulp_riscv_adc_read_channel(ADC_UNIT, ADC_CHANNEL);
    last_reading = raw;

    if (raw > THRESHOLD) {
        wake_count++;
        ulp_riscv_wakeup_main_processor();
    }
    // Return; the ULP halts and the RTC timer re-launches main() next cycle.
    return 0;
}
```

The control flow is the load-bearing detail: **the ULP program contains no `while (1)` loop and no sleep call.** The main CPU configures a wake period; on each RTC-timer tick the coprocessor re-enters `main()` from the top, runs to completion, and halts until the next tick. Because each launch starts a fresh invocation, **all state that must survive between samples lives in globals held in RTC memory** — `wake_count` and `last_reading` here. Local variables do not persist across relaunches.

## The main-CPU side: load, run, sleep

The main firmware compiles the ULP program through the `ulp_embed_binary` CMake helper, loads the blob into RTC memory, starts the coprocessor, arms the ULP wake-up source, and enters deep sleep. The build system generates `ulp_main.h`, which exposes the ULP globals under the `ulp_` prefix as `ulp_last_reading`, `ulp_wake_count` and so on.

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

`app_main` therefore implements a two-state machine keyed on `esp_sleep_get_wakeup_cause()`: **the cold-boot branch installs and starts the coprocessor, and the ULP-wake branch reads the shared variables and performs the real measurement.** Both branches converge on re-arming the wake-up source and re-entering deep sleep, so the ULP is loaded once and survives subsequent sleep cycles.

Division of responsibility for the analogue-to-digital converter (ADC) matters. On first boot the main CPU calls `ulp_riscv_adc_init()` with an `ulp_riscv_adc_cfg_t` naming the unit, channel and attenuation before launching the coprocessor: **the ULP core performs the conversion, but the configuration and calibration are established by the CPU.** Watching a **GPIO level** instead uses the same skeleton — `ulp_riscv_gpio_init(pin)` followed by polling `ulp_riscv_gpio_get_level(pin)` and calling `ulp_riscv_wakeup_main_processor()` on the transition of interest. Espressif's `system/ulp/ulp_riscv/gpio` example follows this shape.

## Current arithmetic

The figures determine whether the arrangement is worthwhile. Deep sleep with only the RTC timer and RTC memory retained sits at **roughly 10 µA or below**; the ULP sensor-monitored pattern — the coprocessor waking on its timer at roughly a 1 % duty cycle — is quoted at **about 100 µA**. A main core that is awake and running draws **tens of milliamps**, three orders of magnitude more, so even the raised ULP floor is far cheaper than polling from the CPU. Exact figures are per-chip and per-module; the datasheet for the specific part is the only reliable source.

The LP-core parts land lower still, and Espressif's LP-core walkthrough shows a C6 running a periodic LP-core task in the low tens of microamps. The LP-core startup path is the same sequence under renamed calls, with the timer period supplied in the configuration structure rather than through a separate setter:

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

The resulting decision rule is a comparison of averages. A frequently firing trigger wakes the main CPU often enough that the ULP's standing overhead is not recovered, and a plain fast RTC-timer wake may consume less. A rare trigger — a threshold crossed once an hour — makes continuous monitoring available for a standing current two to three orders of magnitude below an awake CPU.

**Try next:** flash Espressif's `examples/system/ulp/ulp_riscv/adc` on an S3, or the corresponding `examples/system/ulp/lp_core` example on a C6, place a multimeter in series with the 3.3 V rail, and record average current while the sensor is swept past the threshold; compare against the same node polling from the main CPU.

## Pitfalls

- **A sensor on a pin that is not RTC-capable is unreadable during deep sleep.** The ULP reaches RTC-domain peripherals only; a GPIO outside that domain loses power with the main cores, so the poll returns nothing useful.
- **Expecting local variables to persist across launches produces a monitor with no memory.** The RTC timer re-enters `main()` each cycle rather than resuming it, so only globals in RTC memory carry state; a debounce counter held in a local resets on every tick.
- **Omitting `ulp_riscv_adc_init()` on the CPU side leaves the ADC unconfigured.** The coprocessor reads a channel whose unit, attenuation and calibration were never established, and the raw counts compared against the threshold are not the intended measurement.
- **Omitting `esp_sleep_enable_ulp_wakeup()` before sleeping discards the wake signal.** The ULP still detects the event and still calls `ulp_riscv_wakeup_main_processor()`, but the wake-up source is not armed and the CPU stays asleep.
- **Re-running `start_ulp()` on every boot re-loads the binary and clears accumulated state.** Guarding the branch on `esp_sleep_get_wakeup_cause()` is what keeps `wake_count` meaningful across sleep cycles.
- **A trigger that fires frequently inverts the saving.** At high event rates the ULP's standing current is added to main-CPU wake-ups that would have happened anyway, and average current exceeds the timer-polled arrangement it replaced.
