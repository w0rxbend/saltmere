---
title: "LoRaWAN for an air-quality node: kilometers of range on a tiny power budget"
date: 2026-07-31
track: iot-embedded
summary: "Wi-Fi is fine until your sensor is a field away from the nearest AP. An ESP32-S3 paired with an SX1262 radio joins The Things Network over LoRaWAN and sends PM readings kilometers, on microamps — as long as you respect the duty cycle and pack your payload into a handful of bytes."
reading_time: 6
tags: [esp32, lorawan, sx1262, radiolib, the-things-network, low-power]
sources:
  - title: "LoRaWAN Sensor Node — Wio-SX1262 with XIAO ESP32-S3 (Seeed Studio Wiki)"
    url: "https://wiki.seeedstudio.com/wio_sx1262_xiao_esp32s3_for_lora_sensor_node/"
  - title: "RadioLib — universal wireless communication library (LoRaWAN support)"
    url: "https://github.com/jgromes/RadioLib"
  - title: "The Things Network — LoRaWAN duty cycle and Fair Use Policy"
    url: "https://www.thethingsnetwork.org/docs/lorawan/duty-cycle/"
---

The Wi-Fi + MQTT air-quality node is a great design when there's an access point nearby. Put the sensor at the far end of a field, a vineyard, or an industrial yard and it falls apart: no AP in range, and Wi-Fi's power draw makes battery life miserable anyway. **LoRaWAN** trades bandwidth for reach — a few hundred bytes per message, but kilometers of range and a radio that sleeps at microamps between transmissions. Pair an ESP32-S3 with a Semtech **SX1262** transceiver and your PM2.5 readings travel far on very little.

## The trade you're accepting

LoRa buys distance with a slow, robust modulation, and LoRaWAN layers duty-cycle rules on top. On the EU868 band that's roughly a **1% duty cycle** per sub-band, and The Things Network adds a Fair Use Policy of about **30 seconds of airtime per device per day**. At a middling spreading factor a single small uplink is tens to a couple hundred milliseconds of airtime, so you get on the order of a few dozen to a couple hundred messages a day — nowhere near "every 3 seconds." This reframes the whole node: **sample often, transmit rarely, and make each payload count.** Average readings locally, send a summary every few minutes.

## Pack the payload, don't JSON it

You cannot afford `{"pm25":12.3}` here — every byte is airtime. Bit-pack instead. PM values fit fine as scaled 16-bit integers:

```c
// 6-byte uplink: pm2.5, pm10, temperature — all scaled x10
uint8_t buf[6];
uint16_t pm25 = (uint16_t)(pm25_f * 10);   // 12.3 ug/m3 -> 123
uint16_t pm10 = (uint16_t)(pm10_f * 10);
int16_t  temp = (int16_t)(temp_f * 10);    // -5.0 C -> -50
buf[0] = pm25 >> 8; buf[1] = pm25 & 0xFF;
buf[2] = pm10 >> 8; buf[3] = pm10 & 0xFF;
buf[4] = temp >> 8; buf[5] = temp & 0xFF;
```

Six bytes carries a full reading; you unpack it in a TTN payload formatter on the server side. (If you'd rather not hand-roll it, the Cayenne LPP format does the same job with a self-describing byte layout.)

## Joining and sending with RadioLib

RadioLib gives the SX1262 a clean LoRaWAN API. Use **OTAA** (over-the-air activation) — you register the device on TTN, copy the JoinEUI, DevEUI and AppKey into the sketch, and the network hands out session keys on join:

```cpp
#include <RadioLib.h>
SX1262 radio = new Module(/*CS*/8, /*DIO1*/14, /*RST*/12, /*BUSY*/13);
LoRaWANNode node(&radio, &EU868);

void setup() {
  radio.begin();
  node.beginOTAA(joinEUI, devEUI, nwkKey, appKey);
  node.activateOTAA();               // performs the join handshake
}

void loop() {
  uint8_t buf[6];
  fill_payload(buf);                 // the packing above
  node.sendReceive(buf, sizeof(buf)); // uplink (and pick up any downlink)
  esp_deep_sleep(5 * 60 * 1000000ULL); // sleep 5 min; radio idles at uA
}
```

The `deep_sleep` between sends is what makes the power budget work — the ESP32-S3 and the SX1262 both drop to microamps, so the node lives for months on a small pack. Persist the LoRaWAN session (DevNonce, frame counters) in RTC memory or NVS across deep sleep, or you'll re-join needlessly and burn your airtime allowance on join requests.

## When LoRaWAN is the wrong answer

Be honest about the ceiling. If you need sub-second updates, firmware OTA over the same link, or rich per-sample payloads, LoRaWAN's airtime budget simply won't allow it — that's a Wi-Fi or cellular job. LoRaWAN shines for *low-rate, long-range, long-life* telemetry: a scattering of remote sensors reporting a compact summary every few minutes. Match the radio to that shape and it's unbeatable; fight it and you'll spend your day arguing with the duty cycle.

**Try next:** register one device on The Things Network, flash the OTAA sketch above, and watch the join + first uplink land in the TTN console. Then write the server-side payload formatter that turns your 6 bytes back into pm2.5/pm10/temp — decoding your own packed bytes is the moment the bandwidth math stops being abstract.
