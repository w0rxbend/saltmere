---
title: "LoRaWAN for an air-quality node: kilometers of range on a tiny power budget"
date: 2026-07-31
track: iot-embedded
summary: "Wi-Fi is adequate only while a sensor sits within reach of an access point. An ESP32-S3 paired with an SX1262 radio joins The Things Network over LoRaWAN and carries particulate readings kilometers on a microamp idle budget — provided the duty cycle is respected and the payload is packed into a handful of bytes."
reading_time: 7
tags: [esp32, lorawan, sx1262, radiolib, the-things-network, low-power]
sources:
  - title: "LoRaWAN Sensor Node — Wio-SX1262 with XIAO ESP32-S3 (Seeed Studio Wiki)"
    url: "https://wiki.seeedstudio.com/wio_sx1262_xiao_esp32s3_for_lora_sensor_node/"
  - title: "RadioLib — universal wireless communication library (LoRaWAN support)"
    url: "https://github.com/jgromes/RadioLib"
  - title: "The Things Network — LoRaWAN duty cycle and Fair Use Policy"
    url: "https://www.thethingsnetwork.org/docs/lorawan/duty-cycle/"
---

**Gist.** An air-quality node placed at the far end of a field, a vineyard or an industrial yard has no access point in range, and Wi-Fi's transmit and association power draw would exhaust a small battery regardless. LoRaWAN — a long-range wide-area network protocol layered on Semtech's LoRa chirp modulation — reaches that distance with a radio that idles at microamps between transmissions. **The cost is airtime: regulatory duty-cycle limits and network fair-use rules cap a node at a few dozen to a couple of hundred short messages per day**, which forces local aggregation and a byte-counted payload.

## The airtime budget is the binding constraint

LoRa buys distance with a slow, robust modulation; LoRaWAN adds regional access rules on top. On the EU868 band the limit is approximately a **1% duty cycle per sub-band** — after transmitting for one time unit, the node must stay off that sub-band for roughly ninety-nine. The Things Network (TTN) layers a Fair Use Policy of about **30 seconds of uplink airtime per device per day** on top of the regulatory ceiling.

Airtime is not a constant: it grows with the spreading factor, the parameter that trades data rate for link budget. At a middling spreading factor a single short uplink occupies tens to a couple of hundred milliseconds. Dividing the daily fair-use allowance by that figure gives **on the order of a few dozen to a couple of hundred uplinks per day** — two to three orders of magnitude short of a reading every three seconds.

That arithmetic reframes the node rather than merely constraining it. **Sampling and transmission are decoupled**: the particulate sensor is read on its own schedule, readings are averaged in RAM, and only a summary leaves the node every few minutes. A node that transmits on every sample will either be throttled by the stack's duty-cycle bookkeeping or exceed the fair-use policy.

## Packing the payload

A JSON encoding such as `{"pm25":12.3}` costs thirteen bytes for one measurement, and **every byte is airtime**. Fixed-point integers carry the same information in a fraction of the space: particulate concentrations and temperature scaled by ten fit in 16 bits each.

```c
// 6-byte uplink: pm2.5, pm10, temperature — all scaled x10
uint8_t buf[6];
uint16_t pm25 = (uint16_t)(pm25_f * 10);   // 12.3 ug/m3 -> 123
uint16_t pm10 = (uint16_t)(pm10_f * 10);
int16_t  temp = (int16_t)(temp_f * 10);    // -5.0 C -> -50
buf[0] = pm25 >> 8; buf[1] = pm25 & 0xFF;  // big-endian: high byte first
buf[2] = pm10 >> 8; buf[3] = pm10 & 0xFF;
buf[4] = temp >> 8; buf[5] = temp & 0xFF;
```

Three quantities occupy **six bytes**. The layout is implicit: nothing in the frame states which field is which, so the byte order and scaling factor form an **unversioned contract between firmware and the TTN payload formatter** that decodes it on the server side. Changing the field order or the scale without updating the formatter yields decoded values that are wrong but structurally plausible — a silent corruption rather than a parse error. Cayenne Low Power Payload (LPP) avoids that coupling with a self-describing type-and-channel byte per field, at the cost of those extra bytes of airtime.

Note the sign handling: `temp` is a signed 16-bit value, and the shift and mask above copy its two's-complement representation byte for byte. The decoder must reinterpret those bytes as signed; treating them as unsigned turns −5.0 °C into a large positive number.

## Joining and sending with RadioLib

RadioLib exposes the SX1262 through a LoRaWAN node abstraction. Over-the-air activation (OTAA) is the join procedure in which the device is registered on TTN with a JoinEUI, DevEUI and AppKey, and **both sides derive the session keys during the join exchange** from the AppKey and the nonces carried in the join request and join accept — as opposed to activation by personalization, where session keys are burned into the firmware.

```cpp
#include <RadioLib.h>
SX1262 radio = new Module(PIN_CS, PIN_DIO1, PIN_RST, PIN_BUSY); // board pinout
LoRaWANNode node(&radio, &EU868);

void setup() {
  radio.begin();
  node.beginOTAA(joinEUI, devEUI, nwkKey, appKey);
  node.activateOTAA();               // performs the join handshake
}

void loop() {
  uint8_t buf[6];
  fill_payload(buf);                 // the packing above
  node.sendReceive(buf, sizeof(buf)); // uplink, then open the downlink windows
  esp_deep_sleep(5 * 60 * 1000000ULL); // sleep 5 min; radio idles at uA
}
```

`sendReceive` is a single call because a LoRaWAN class A device transmits first and only then listens: **the uplink opens the receive windows, so a downlink can reach the node only immediately after it has spoken.** There is no way for the network to interrupt a sleeping node; the sleep interval is therefore also the worst-case latency of any command or configuration change sent from the server.

Deep sleep is what makes the power budget close. Between transmissions both the ESP32-S3 and the SX1262 drop to microamps, so the average current is dominated by the brief transmit bursts rather than by the long idle stretches between them.

### Session state must survive the sleep

Deep sleep on the ESP32-S3 resets the CPU and clears ordinary RAM: execution restarts from the beginning of the program. If LoRaWAN session state is held in ordinary variables, **every wake-up looks like a fresh device and triggers another join**.

Two counters make that failure concrete.

- **DevNonce.** Each join request carries a nonce, and the network server rejects a join request whose DevNonce it has already seen from that device — a replay defence. A node that restarts its nonce sequence from the same starting value after every sleep will have its join requests dropped, and the node never rejoins.
- **Frame counters.** Uplink and downlink frame counters must increase monotonically within a session. A counter that resets to zero while the network still expects a higher value causes the network to discard the frames as replays.

Each join attempt also costs airtime drawn from the same daily allowance as the data, so a rejoin loop consumes the budget the payload packing was meant to preserve. The state — session keys, DevNonce, frame counters — belongs in **RTC memory, which the ESP32-S3 keeps powered through deep sleep, or in non-volatile storage (NVS) in flash**, which additionally survives a power cycle at the cost of flash write endurance.

## Where the protocol does not reach

LoRaWAN's airtime budget rules out sub-second update rates, firmware updates over the same link, and per-sample payloads of any richness. Those workloads belong on Wi-Fi or cellular. The shape LoRaWAN fits is **low-rate, long-range, long-life telemetry**: scattered remote sensors reporting a compact summary every few minutes, each one aggregating locally so that the radio speaks rarely.

## Pitfalls

- **Transmitting on every sample.** Symptom: uplinks are silently dropped or delayed by the stack. Cause: the EU868 1% per-sub-band duty cycle forces an off-period after each transmission, and TTN's ~30 s daily fair-use allowance is consumed within minutes.
- **Session state in ordinary RAM.** Symptom: the node rejoins on every wake-up and eventually stops being accepted. Cause: deep sleep clears RAM, the DevNonce sequence restarts, and the network server rejects nonces it has already seen.
- **Frame counters reset without the session.** Symptom: the join succeeds but uplinks never appear in the console. Cause: the network discards frames whose counter is not above the last one seen for that session.
- **Raising the spreading factor to chase range.** Symptom: the daily message quota collapses. Cause: airtime per uplink grows with the spreading factor, so the same fair-use allowance buys proportionally fewer messages.
- **JSON or other self-describing text in the payload.** Symptom: uplinks are several times longer than necessary. Cause: field names and delimiters are transmitted alongside the values, and airtime is charged per byte.
- **Decoding the packed temperature as unsigned.** Symptom: sub-zero temperatures decode as values near 6500 °C. Cause: the two's-complement bytes of a negative `int16_t` are reinterpreted without sign extension.
- **Editing the firmware's byte layout without the payload formatter.** Symptom: plausible but wrong readings in the application. Cause: the packed frame carries no field identifiers or version, so a mismatched decoder cannot detect the change.
