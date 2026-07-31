---
title: "Talking CAN bus from an ESP32 with the built-in TWAI controller"
date: 2026-07-31
track: iot-embedded
summary: "The ESP32 has a CAN 2.0 controller on-chip — Espressif calls it TWAI for trademark reasons. All you add is a cheap transceiver. Here's the wiring, the bit-timing that trips everyone up, and a minimal send/receive so you can pull live data off a vehicle or wire a robust multi-drop sensor bus."
reading_time: 5
tags: [esp32, twai, can-bus, esp-idf, transceiver, sensors]
sources:
  - title: "ESP-IDF Programming Guide — Two-Wire Automotive Interface (TWAI), v6.0"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/twai.html"
  - title: "arduino-esp32 TWAI examples (TWAItransmit / TWAIreceive)"
    url: "https://github.com/espressif/arduino-esp32/blob/master/libraries/ESP32/examples/TWAI/TWAIreceive/TWAIreceive.ino"
  - title: "TI SN65HVD230 3.3-V CAN transceiver datasheet"
    url: "https://www.ti.com/lit/ds/symlink/sn65hvd230.pdf"
  - title: "Bosch CAN Specification 2.0 (standard/extended frames, bit stuffing)"
    url: "http://esd.cs.ucr.edu/webres/can20.pdf"
  - title: "ESP-IDF twai_self_test example"
    url: "https://github.com/espressif/esp-idf/tree/master/examples/peripherals/twai/twai_self_test"
---

CAN bus is the quiet workhorse of anything with wheels or a factory floor: two wires, differential signalling, arbitration built into the protocol so nodes never corrupt each other, and it shrugs off electrical noise that would wreck I2C over any real distance. If you have an air-quality node in a garage, a robot with limbs, or you want to sniff a car's OBD-II port, CAN is the right bus. The good news is the ESP32 already has a CAN 2.0 controller inside it. Espressif can't call it "CAN" (trademark), so in the docs it's the **TWAI** — Two-Wire Automotive Interface. Same thing.

## The one part you must add: a transceiver

The ESP32's TWAI controller speaks the CAN *protocol*, but its pins are plain 3.3 V logic. A CAN bus runs a differential pair (CAN_H / CAN_L) at higher swing. So **you need an external transceiver** between the ESP32 and the bus — there is no on-chip one. The standard choice for 3.3 V is the **TI SN65HVD230** (or the MCP2551 if you level-shift). Wiring:

```
ESP32 GPIO21 (TX) ----> transceiver TXD
ESP32 GPIO22 (RX) <---- transceiver RXD
transceiver CANH/CANL --> the two-wire bus
120 Ω termination resistor at EACH physical end of the bus
```

The two `120 Ω` terminators are not optional. Miss them and you'll get reflections and random bit errors that look like software bugs but aren't.

## Bit timing is where projects die

Every node on a CAN bus must agree on the bitrate *exactly*, and TWAI configures this through three structs. ESP-IDF ships convenience macros so you don't hand-calculate the segments:

```c
#include "driver/twai.h"

// GPIO for TX/RX, and normal (vs listen-only / self-test) mode
twai_general_config_t g = TWAI_GENERAL_CONFIG_DEFAULT(
        GPIO_NUM_21, GPIO_NUM_22, TWAI_MODE_NORMAL);
twai_timing_config_t  t = TWAI_TIMING_CONFIG_500KBITS();   // 500 kbit/s
twai_filter_config_t  f = TWAI_FILTER_CONFIG_ACCEPT_ALL(); // no HW filtering
```

`500 kbit/s` is the common OBD-II rate; the macro family also gives you `_250KBITS`, `_1MBITS`, etc. The rule that catches everyone: **the whole bus must use one bitrate.** A node set to 250k on a 500k bus doesn't get garbled data — it gets *nothing*, because it never sees a valid frame boundary. Check this first when a node goes silent.

## Install, start, send, receive

```c
void app_main(void) {
    ESP_ERROR_CHECK(twai_driver_install(&g, &t, &f));
    ESP_ERROR_CHECK(twai_start());

    // --- transmit ---
    twai_message_t tx = {
        .identifier = 0x123,     // 11-bit standard ID
        .extd = 0,               // set 1 for 29-bit extended IDs
        .data_length_code = 4,   // 0..8 bytes
        .data = {0xDE, 0xAD, 0xBE, 0xEF},
    };
    twai_transmit(&tx, pdMS_TO_TICKS(1000));   // queues; blocks up to 1s

    // --- receive ---
    twai_message_t rx;
    if (twai_receive(&rx, pdMS_TO_TICKS(1000)) == ESP_OK) {
        printf("id=0x%lx dlc=%d  %02x %02x %02x %02x\n",
               rx.identifier, rx.data_length_code,
               rx.data[0], rx.data[1], rx.data[2], rx.data[3]);
    }
}
```

A CAN data frame carries at most **8 bytes** (`data_length_code` 0–8) — that limit is fundamental to classic CAN 2.0, and it shapes how you design messages: pack a couple of sensor readings per frame, don't try to stream. The 11-bit **identifier** doubles as the arbitration priority: **lower ID wins the bus**, so put your urgent messages on low IDs. (Extended 29-bit IDs exist via `.extd = 1` when 2048 standard IDs aren't enough.)

## Reading the bus health, and a safety valve

TWAI implements CAN's error-confinement automatically: a node that keeps failing to transmit climbs its error counters and eventually drops to **bus-off**, going silent to protect the bus. Watch for it and recover:

```c
twai_status_info_t s;
twai_get_status_info(&s);
if (s.state == TWAI_STATE_BUS_OFF) {
    twai_initiate_recovery();   // then wait for TWAI_STATE_STOPPED, restart
}
```

If you're just *listening* to a car and never want to perturb the bus, install with `TWAI_MODE_LISTEN_ONLY` — the controller ACKs nothing and can't transmit, so you can sniff OBD-II traffic with zero risk of injecting a frame. (Newer ESP-IDF v6 also ships a redesigned node-style driver in `esp_twai.h`; the `driver/twai.h` API shown here remains supported and is what the Arduino core wraps.)

**Try next:** wire one ESP32 + SN65HVD230 to itself in `TWAI_MODE_NO_ACK` and run the `twai_self_test` example — it transmits and receives its own frames with no second node, proving your timing and transceiver wiring before you ever touch a real bus. Then bump to two boards at 500k and send a fake "PM2.5" reading from one to the other; you now have a noise-immune multi-drop sensor bus.
