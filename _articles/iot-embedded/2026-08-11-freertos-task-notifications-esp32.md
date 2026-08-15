---
title: "FreeRTOS task notifications on the ESP32: signalling a task without a semaphore"
date: 2026-08-11
track: iot-embedded
summary: "Allocating a binary semaphore so that an interrupt handler can report 'the sensor is ready' spends RAM and cycles on a kernel object that carries one bit. Every FreeRTOS task already owns a 32-bit notification value; the documentation reports that signalling it directly is up to 45% faster and uses less RAM. This covers the API, the interrupt-safe path, and the single-recipient limitation."
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

**Gist.** Unblocking one task from an interrupt service routine (ISR) through a binary semaphore allocates a separate kernel object and routes every give and take through that object's queue machinery, to carry a single bit of "an event occurred". FreeRTOS gives every task a **32-bit notification value and a notification state inside its Task Control Block (TCB)**, so a sender can wake the task directly; the FreeRTOS documentation reports this path is **up to 45% faster and uses less RAM** than the semaphore equivalent. The cost is that a notification has **exactly one recipient** and no broadcast: any event that must fan out to several waiters still needs a semaphore or an event group.

## The state carried in the Task Control Block

A task's notification is two fields, not one. The **notification value** is an unsigned 32-bit word. The **notification state** is a small state machine with three positions: *not waiting*, *waiting for a notification*, and *notification pending*. A send sets the state to pending and folds the sender's value into the notification value; a receive that finds the state already pending returns immediately and does not block, while a receive that finds no pending notification moves the task to the blocked list until a send arrives or the timeout expires.

Two consequences follow directly from that machine. First, **a notification sent before the receiver calls the take is not lost** — it is latched in the pending state, so an ISR that fires while the task is busy elsewhere still wakes it on the next call. Second, because the pending flag is a single bit rather than a count, **the number of events retained is bounded by whatever the notification value itself encodes**, not by the state machine.

## Counter mode: give and take

The narrowest use replaces a binary or counting semaphore. `xTaskNotifyGive()` increments the target task's notification value; `ulTaskNotifyTake()` blocks until that value is non-zero, then either clears or decrements it:

```c
// Task waits as it would on a semaphore:
uint32_t events = ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
```

The first argument selects which of those two happens, and it decides whether events can be dropped. Passing `pdTRUE` **clears the notification value to zero** on return: binary-semaphore behaviour, one wake per call regardless of how many sends accumulated. Passing `pdFALSE` **decrements the value by one**, which makes the same call a *counting* semaphore — three sends that arrived while the task was busy produce three returns before the task blocks again. **The count is held in the 32-bit notification value**, so `pdFALSE` is the mode to use when every edge must be serviced. No creation call, no handle to store, no object to leak.

## The general API and its action modes

`ulTaskNotifyTake` interprets the word only as a counter. To carry data, or to use the word as a set of flags, `xTaskNotify()` and `xTaskNotifyWait()` take an `eNotifyAction` that tells the kernel how to fold the sender's value into the target's notification word:

| Action | Effect on the notification value |
|---|---|
| `eSetBits` | OR the bits in — event-group-style flags in a single word |
| `eIncrement` | Add one (the operation `xTaskNotifyGive` performs) |
| `eSetValueWithOverwrite` | Overwrite unconditionally, like a mailbox of length one |
| `eSetValueWithoutOverwrite` | Write only if the target had no unread pending value; fails otherwise |
| `eNoAction` | Wake the task, leave the value untouched |

`eSetBits` supplies **up to 32 independent event flags in one word** without allocating an event group, and `xTaskNotifyWait` accepts bitmasks to clear on entry and on exit, so the waiter can consume the specific flags it handles and leave the rest set.

The two value-setting actions differ only in their failure behaviour, and that difference is the whole reason to pick one. `eSetValueWithOverwrite` always succeeds and **discards the previous value if the receiver has not yet read it**, which suits a mailbox holding the latest sample. `eSetValueWithoutOverwrite` **returns `pdFAIL` rather than clobbering an unread value**, which suits a producer that must detect that the consumer has fallen behind. A caller that ignores the return code of the second form has chosen the first form with extra steps.

## From an ISR: the FromISR variants

Interrupt context requires the `...FromISR` forms. They return a "higher priority task woken" flag through an out-parameter so that the handler can request a context switch on the way out; on ESP-IDF that request is `portYIELD_FROM_ISR()`. **Omitting the yield does not lose the notification — it delays the wake until the next scheduler tick**, which converts a sub-microsecond handoff into a latency bounded by the tick period. The following is a data-ready (DRDY) driven read, the glue for a sensor with an interrupt pin; the sensor read itself uses the oneshot and I2C patterns from the [ESP32 ADC calibration article](/articles/iot-embedded/2026-07-30-esp32-adc-oneshot-calibration/):

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
        // Blocked, consuming no CPU, until the ISR signals.
        ulTaskNotifyTake(pdFALSE, portMAX_DELAY);  // pdFALSE: decrement, so bursts are not collapsed
        // DRDY fired — the I2C read belongs here, not in the handler.
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

The handler performs no I2C transfer, no logging and no floating-point work: it posts the notification and yields. The task handle is written by `xTaskCreate` **before** the ISR service is installed, which is the ordering that keeps `s_reader_task` from being `NULL` when the first edge arrives.

## The single-recipient limitation

`vTaskNotifyGiveFromISR` takes one `TaskHandle_t`. There is no broadcast form: a notification wakes the task named in the call and no other. An event that two or three tasks must observe therefore needs a different primitive — an event group, or a semaphore per waiter. The related restriction is directional: **a notification can be sent to a task but not to an ISR**, and a task that sends a notification cannot block waiting for the recipient to accept it, so the send never applies back-pressure.

## Dual-core notes on ESP-IDF

ESP-IDF ships its own FreeRTOS, based on the vanilla FreeRTOS kernel with modifications for symmetric multiprocessing (SMP) across the ESP32's two cores. Task notifications are SMP-safe there: the sending ISR and the receiving task may run on different cores, and the kernel performs the cross-core wake. A general-purpose input/output (GPIO) ISR runs on whichever core installed the ISR service, and the notification still reaches its target task wherever that task is scheduled.

## Pitfalls

- **Passing `pdTRUE` to `ulTaskNotifyTake` in a burst-prone path drops events.** The clear-on-exit form zeroes the value, so N interrupts that arrive before the task runs produce one wake, not N.
- **Ignoring the return value of `xTaskNotify` with `eSetValueWithoutOverwrite` silently discards the send.** That action returns `pdFAIL` when the previous value is unread, and unlike the overwrite form it makes no change to the notification value.
- **Installing the ISR handler before `xTaskCreate` returns leaves the task handle `NULL`.** An edge arriving in that window passes a null `TaskHandle_t` into the notify call.
- **Discarding the "higher priority task woken" flag instead of feeding it to `portYIELD_FROM_ISR` postpones the wake to the next tick.** The symptom is a wake latency floor at the tick period rather than a lost event.
- **Reaching for a notification when several tasks must observe the same event wakes exactly one of them.** The API has no broadcast; the others remain blocked.
- **A second use of the same task's notification value inside a driver collides with the first.** The value lives in the TCB, so two unrelated protocols sharing one task share one 32-bit word and one pending flag.
