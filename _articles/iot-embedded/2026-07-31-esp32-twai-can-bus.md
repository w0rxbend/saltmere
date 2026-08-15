---
title: "CAN bus on the ESP32 through the built-in TWAI controller"
date: 2026-07-31
track: iot-embedded
summary: "The ESP32 carries an on-chip CAN 2.0 controller, named TWAI in Espressif documentation. Only an external transceiver is added. This covers the wiring, the bit-timing agreement the bus depends on, and a minimal transmit/receive path."
reading_time: 6
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

**Gist.** A multi-drop sensor or vehicle bus needs collision-free media access and immunity to electrical noise over metres of cable, which single-ended buses such as I2C do not provide. Controller Area Network (CAN) supplies both: a differential pair carries the signal, and non-destructive bitwise arbitration on the frame identifier resolves simultaneous transmissions without corrupting either frame. The cost is a payload ceiling of **eight bytes per classic CAN 2.0 data frame**, a bitrate every node must share exactly, and an external transceiver the ESP32 does not integrate.

Espressif documents the ESP32's on-chip CAN 2.0 controller as the **Two-Wire Automotive Interface (TWAI)**; the peripheral implements the CAN protocol described in the Bosch CAN Specification 2.0.

## The transceiver is a required part

The TWAI controller emits and samples plain 3.3 V logic on two pins, TX and RX. The bus itself is a differential pair, CAN_H and CAN_L, driven at a larger swing. **No transceiver is integrated on the ESP32**, so an external one converts between the two domains. The **TI SN65HVD230** is a 3.3 V-supply part that pairs directly; the MCP2551 is a 5 V part and requires level shifting on the receive path.

```
ESP32 GPIO21 (TX) ----> transceiver TXD
ESP32 GPIO22 (RX) <---- transceiver RXD
transceiver CANH/CANL --> the two-wire bus
120 Ω termination resistor at EACH physical end of the bus
```

The two `120 Ω` terminators are load-bearing. **Termination at both physical ends only** — not at intermediate nodes, and not a single resistor — matches the cable impedance; without it the driven edge reflects off the unterminated end and returns to corrupt the sample point, producing intermittent bit errors whose symptom (frames failing checksum, error counters climbing) resembles a firmware fault.

## Bit timing, and why a mismatch produces silence rather than noise

CAN has no separate clock line. Receivers recover timing from edges in the bit stream, which the transmitter guarantees by **bit stuffing**: after five consecutive bits of the same polarity, a complementary bit is inserted. Every node therefore has to divide its own clock into the same nominal bit time and place its sample point compatibly. ESP-IDF supplies timing macros so the segment values are not hand-computed:

```c
#include "driver/twai.h"

// GPIO for TX/RX, and normal (vs listen-only / self-test) mode
twai_general_config_t g = TWAI_GENERAL_CONFIG_DEFAULT(
        GPIO_NUM_21, GPIO_NUM_22, TWAI_MODE_NORMAL);
twai_timing_config_t  t = TWAI_TIMING_CONFIG_500KBITS();   // 500 kbit/s
twai_filter_config_t  f = TWAI_FILTER_CONFIG_ACCEPT_ALL(); // no HW filtering
```

`500 kbit/s` is the common OBD-II rate; the macro family also provides `_250KBITS` and `_1MBITS`, among others. **The entire bus must run one bitrate.** A node configured for 250 kbit/s on a 500 kbit/s bus does not deliver garbled payloads to the application — it delivers no frames at all, while its receive path registers bit, stuff and form errors and its error counters climb. In normal mode those errors are signalled on the bus as error frames, so a mis-timed node also disturbs the traffic between the nodes that agree. A node that has gone silent is worth checking against this before any protocol-level debugging.

## Install, start, transmit, receive

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

`twai_transmit` enqueues the frame; the timeout bounds how long the call blocks when the transmit queue is full, not how long the frame takes to win arbitration. A driver install takes three separate configuration structures — general (pins, mode, queue depths), timing, and acceptance filter — and all three are fixed at install time.

### The identifier is the priority

A classic CAN 2.0 data frame carries **at most 8 bytes**, encoded in `data_length_code` (0–8). The limit is a property of the frame format, so a design that needs to move more than 8 bytes must fragment across frames at the application layer; packing a small number of sensor readings per frame fits the format, streaming does not.

The 11-bit standard **identifier doubles as the arbitration key**. Arbitration is bitwise and non-destructive: nodes transmit the identifier while monitoring the bus, and a node that sends a recessive bit but reads a dominant one loses and stops, leaving the winner's frame intact and uninterrupted. Because dominant bits win, **the numerically lower identifier takes the bus**, so latency-critical messages belong on low identifiers. Setting `.extd = 1` selects the 29-bit extended identifier format when the 11-bit space is insufficient.

## Error confinement and the listen-only escape hatch

TWAI implements CAN's error confinement without application involvement. Each node maintains transmit and receive error counters; repeated transmit failures drive the counters up, and past the confinement thresholds the node reaches **bus-off**, where it stops participating entirely so that one faulty node cannot hold the bus down. Bus-off is not self-clearing from the application's point of view — recovery is requested explicitly:

```c
twai_status_info_t s;
twai_get_status_info(&s);
if (s.state == TWAI_STATE_BUS_OFF) {
    twai_initiate_recovery();   // then wait for TWAI_STATE_STOPPED, restart
}
```

Recovery moves the driver toward `TWAI_STATE_STOPPED`; the driver is then restarted to rejoin the bus. Firmware that never polls `twai_get_status_info` observes a node that has silently stopped transmitting with no error returned by later calls.

For passive observation of a vehicle bus, installing with `TWAI_MODE_LISTEN_ONLY` prevents the controller from transmitting and from acknowledging received frames, so a sniffer cannot inject a frame or alter the acknowledgement of another node's frame. Recent ESP-IDF releases additionally ship a redesigned node-style driver in `esp_twai.h`; the `driver/twai.h` API shown here is the older one, and is the API the Arduino core wraps.

The `twai_self_test` example validates timing and transceiver wiring before a second node exists: in `TWAI_MODE_NO_ACK` a single board transmits and receives its own frames, because that mode does not require an acknowledgement from another node.

## Pitfalls

- **A node delivers no frames at all rather than corrupted data.** Its bitrate differs from the bus bitrate, so reception fails outright and the error counters climb; a mismatch never degrades gracefully.
- **Intermittent bit errors that move when the cable is touched.** Missing or misplaced `120 Ω` termination — terminators belong at the two physical ends of the bus only, and adding a third at a stub node is as damaging as omitting one.
- **A node stops transmitting and no API call reports an error.** Error confinement has driven it to `TWAI_STATE_BUS_OFF`; without polling `twai_get_status_info` and calling `twai_initiate_recovery`, it stays silent for the life of the process.
- **Recovery appears to complete but traffic never resumes.** `twai_initiate_recovery` leaves the driver stopped; the restart step is separate.
- **A sniffer perturbs the bus it is observing.** Installing in `TWAI_MODE_NORMAL` makes the controller acknowledge every well-formed frame it receives, which is a transmission onto the bus; `TWAI_MODE_LISTEN_ONLY` suppresses it.
- **A self-test in `TWAI_MODE_NORMAL` fails with no second node present.** Normal mode requires an acknowledgement from another node; `TWAI_MODE_NO_ACK` is what makes the single-board loopback complete.
- **An 8-byte payload assumption breaks on a larger reading.** `data_length_code` is capped at 8 by the classic CAN 2.0 frame format, so oversized messages must be fragmented in application code rather than configured away.
