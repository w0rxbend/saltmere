---
title: "FreeRTOS task notifications on the ESP32: signal a task without a semaphore"
date: 2026-08-11
track: iot-embedded
summary: "Spinning up a whole binary semaphore just so an ISR can tell a task 'the sensor is ready' costs RAM and cycles you don't need to spend. Every FreeRTOS task already carries a built-in 32-bit notification value — signalling it directly is up to 45% faster and lighter. Here's the API, the ISR-safe path, and the one limitation that will bite you."
reading_time: 6
tags: [esp32, freertos, esp-idf, interrupts, rtos, task-notifications]
sources:
  - title: "RTOS Task Notifications — FreeRTOS Kernel documentation"
    url: "https://www.freertos.org/Documentation/02-Kernel/02-Kernel-features/03-Direct-to-task-notifications/01-Task-notifications"
  - title: "How to Use Task Notifications Within an RTOS — HighIntegritySystems (FreeRTOS tutorial)"
    url: "https://www.highintegritysystems.com/rtos/what-is-an-rtos/rtos-tutorials/how-to-use-task-notifications-for-rtos/"
  - title: "FreeRTOS (IDF) — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/freertos_idf.html"
  - title: "FreeRTOS Task Notifications: A Lightweight Method for Waking Threads — Embedded Artistry"
    url: "https://embeddedartistry.com/blog/2018/05/28/freertos-task-notifications-a-lightweight-method-for-waking-threads/"
---

Here's a pattern you have written a hundred times: a sensor pulls its data-ready (DRDY) line high, a GPIO interrupt fires, and you need to unblock the task that will go read the sample over I2C. The textbook answer is a binary semaphore — `xSemaphoreCreateBinary()` in setup, `xSemaphoreGiveFromISR()` in the handler, `xSemaphoreTake(sem, portMAX_DELAY)` in the task. It works. But you just allocated a whole kernel object whose only job is to carry a single bit of "something happened," and every give/take pays the cost of routing through that object's queue machinery.

FreeRTOS has a lighter primitive built for exactly this: **direct-to-task notifications**. Every task already owns a 32-bit notification value and a notification state, allocated as part of its Task Control Block. If only one task needs to be woken, you can signal it with no separate semaphore, queue, or event group at all. Per the FreeRTOS documentation, unblocking a task this way is **up to 45% faster and uses less RAM** than doing it through a binary semaphore. On an ESP32 juggling Wi-Fi, sensor polling, and an MQTT loop, that saved RAM and those saved cycles add up.

## The lightweight semaphore: give and take

The simplest use replaces a binary or counting semaphore. `xTaskNotifyGive()` increments the target task's notification value; `ulTaskNotifyTake()` blocks until that value is non-zero, then either decrements it or clears it to zero:

```c
// Task waits like it would on a semaphore:
uint32_t events = ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
```

The first argument is the interesting one. Pass `pdTRUE` and the notification value is **cleared to zero** on return — that's binary-semaphore behaviour, one wake per event. Pass `pdFALSE` and it is **decremented by one**, which turns the same call into a *counting* semaphore: if three interrupts fired while you were busy, three calls return before the task blocks again, so you never silently drop events. No `xSemaphoreCreate*`, no handle to store, no object to leak.

## The general API and its action modes

`ulTaskNotifyTake` only sees a counter. When you want to pass actual data or use the value as a set of flags, reach for `xTaskNotify()` / `xTaskNotifyWait()`, which take an `eNotifyAction` telling the kernel how to fold your value into the target's notification word:

| Action | Effect on the notification value |
|---|---|
| `eSetBits` | OR the bits in — event-group-style flags in a single word |
| `eIncrement` | Add one (this is what `xTaskNotifyGive` does under the hood) |
| `eSetValueWithOverwrite` | Overwrite unconditionally, like a mailbox of length one |
| `eSetValueWithoutOverwrite` | Write only if the task hasn't read the previous value; fails otherwise |
| `eNoAction` | Wake the task, leave the value untouched |

`eSetBits` is the useful trick: one 32-bit word gives you up to 32 independent event flags without allocating an event group, and the waiting task can clear specific bits on entry and exit.

## From an ISR: the FromISR variants

Interrupt context needs the `...FromISR` forms, which return a "higher priority task woken" flag so you can request a context switch on the way out. On ESP-IDF that means `portYIELD_FROM_ISR()`. Here is a complete DRDY-driven read, the kind of glue you'd wire to an SCD41 or an accelerometer's interrupt pin — the sensor read itself would use the same oneshot/I2C patterns from the [ESP32 ADC calibration article](/articles/iot-embedded/2026-07-30-esp32-adc-oneshot-calibration/):

```c
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"

#define DRDY_GPIO  GPIO_NUM_4
static TaskHandle_t s_reader_task = NULL;

// Runs in interrupt context — keep it tiny.
static void IRAM_ATTR drdy_isr(void *arg) {
    BaseType_t higher_prio_woken = pdFALSE;
    vTaskNotifyGiveFromISR(s_reader_task, &higher_prio_woken);
    portYIELD_FROM_ISR(higher_prio_woken);
}

static void reader_task(void *arg) {
    for (;;) {
        // Blocks with zero CPU cost until the ISR signals.
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        // DRDY fired — safe to read the sensor over I2C here.
    }
}

void app_main(void) {
    xTaskCreate(reader_task, "reader", 4096, NULL, 5, &s_reader_task);

    gpio_config_t io = {
        .pin_bit_mask = 1ULL << DRDY_GPIO,
        .mode = GPIO_MODE_INPUT,
        .intr_type = GPIO_INTR_POSEDGE,
    };
    gpio_config(&io);
    gpio_install_isr_service(0);
    gpio_isr_handler_add(DRDY_GPIO, drdy_isr, NULL);
}
```

Note the ISR does no I2C, no logging, no floating point — it just posts the notification and yields. All the real work happens in the task, which spends its idle time truly blocked, consuming no CPU.

## The limitation that will catch you

A notification has exactly **one recipient**: `vTaskNotifyGiveFromISR` takes a single `TaskHandle_t`. There is no broadcast. If two or three tasks all need to wake on the same sensor event, task notifications are the wrong tool — a binary semaphore with multiple waiters, or an event group, is what fans one signal out to many. The FreeRTOS docs are blunt that this single-recipient constraint is the trade for the speed, and note it's acceptable because most real designs have exactly one task servicing a given event. Just know the boundary before you architect around it.

## Dual-core notes on ESP-IDF

ESP-IDF ships its own FreeRTOS, based on vanilla FreeRTOS kernel v10.5.1 with modifications for symmetric multiprocessing across the ESP32's two cores. Task notifications are fully SMP-safe here — the giving ISR and the receiving task can run on different cores and the kernel handles the cross-core wake. If your interrupt is pinned to a specific core (GPIO ISRs run on whichever core installed the ISR service), that's fine; the notification still reaches the task wherever it's scheduled. You get the lightweight primitive without giving up dual-core placement.

**Try next:** Swap a binary semaphore in one of your existing sensor drivers for `ulTaskNotifyTake(pdFALSE, ...)` counting mode, then deliberately burst the DRDY line and confirm no events are dropped.
