---
title: "Shrink ESP32 telemetry with Protocol Buffers and nanopb"
date: 2026-08-13
track: iot-embedded
summary: "JSON is fine until you're paying for every byte over cellular or LoRaWAN. Protobuf plus nanopb gives an ESP32 a schema-versioned binary uplink that's a fraction of the size — no malloc, a few KB of flash — and this shows the .proto, the generated C, and the encode-and-publish path over MQTT."
reading_time: 6
tags: [esp32, protobuf, nanopb, mqtt, telemetry]
sources:
  - title: "nanopb — protocol buffers with small code size"
    url: "https://jpa.kapsi.fi/nanopb/"
  - title: "nanopb releases (GitHub)"
    url: "https://github.com/nanopb/nanopb/releases"
  - title: "nanopb concepts & .options reference"
    url: "https://jpa.kapsi.fi/nanopb/docs/concepts.html"
  - title: "Protocol Buffers proto3 language guide"
    url: "https://protobuf.dev/programming-guides/proto3/"
  - title: "Eclipse Mosquitto MQTT broker"
    url: "https://mosquitto.org/"
---

A JSON air-quality reading like `{"pm25":12.4,"co2":812,"ts":1723560000}` is ~40 bytes on the wire and more once you add field names to every message. Multiply by a fleet publishing every few seconds over metered cellular or a duty-cycle-limited LoRaWAN link and the field-name overhead becomes the payload. Protocol Buffers drops the names entirely — fields are identified by integer tags — and **nanopb** is the implementation built for microcontrollers: static allocation, no STL, a couple of KB of code, and no heap if you constrain your fields.

## Why protobuf beats JSON on a constrained uplink

- **Size.** Varint-encoded integers and tag-based fields typically cut payloads 3–5x versus JSON. A reading like the one above lands in ~15 bytes.
- **Schema evolution.** Add a field with a new tag number and old and new firmware still interoperate — new fields are ignored by old readers, missing fields read as defaults. No fragile "if key exists" JSON parsing on the backend.
- **Determinism.** No float-to-string ambiguity, no locale, fixed parsing cost — good for a backend ingesting millions of messages.

The tradeoff: payloads aren't human-readable, and both ends must share the `.proto`. For fleet telemetry that's a feature, not a cost.

## Define the message

```protobuf
syntax = "proto3";

message SensorReading {
  uint32 device_id = 1;
  uint32 timestamp = 2;   // unix seconds
  float  pm25      = 3;   // µg/m³
  uint32 co2       = 4;   // ppm
  sint32 temp_c10  = 5;   // deci-degrees, signed zigzag
}
```

Use `sint32` for values that go negative (zigzag encoding keeps small magnitudes small). Scaling temperature to deci-degrees as an int avoids shipping a float where you don't need one.

## Generate C with nanopb

Grab nanopb (current stable **0.4.9.1**, released 1 Dec 2024) — it bundles a `protoc` plugin. The generator reads an optional `.options` file that pins field sizes so nothing is dynamically allocated:

```bash
# sensor.options — no malloc, fixed buffers only
# (only needed if you add string/bytes fields, e.g.:)
# SensorReading.fw_version  max_size:16

python -m nanopb.generator sensor.proto
# -> sensor.pb.h and sensor.pb.c
```

Add `sensor.pb.c`, `pb_encode.c`, `pb_decode.c`, and `pb_common.c` to your build. With only scalar fields, the generated struct is a flat, fixed-size C struct — safe to put on the stack.

## Encode and publish over MQTT

```cpp
#include <pb_encode.h>
#include "sensor.pb.h"
#include <PubSubClient.h>

extern PubSubClient mqtt;  // already connected

bool publishReading(uint32_t id, float pm25, uint32_t co2, int32_t tC10) {
  SensorReading msg = SensorReading_init_zero;
  msg.device_id = id;
  msg.timestamp = (uint32_t)(time(nullptr));
  msg.pm25      = pm25;
  msg.co2       = co2;
  msg.temp_c10  = tC10;

  uint8_t buf[64];
  pb_ostream_t os = pb_ostream_from_buffer(buf, sizeof(buf));
  if (!pb_encode(&os, SensorReading_fields, &msg)) {
    Serial.printf("encode failed: %s\n", PB_GET_ERROR(&os));
    return false;
  }
  // os.bytes_written is the exact payload length — publish raw bytes
  return mqtt.publish("fleet/node-kitchen-01/tele", buf, os.bytes_written, false);
}
```

`pb_ostream_from_buffer` writes into a stack buffer; `os.bytes_written` is the true length (protobuf is not fixed-width). Publish that slice as a binary MQTT payload — don't `String`-ify it. Retain stays `false` for telemetry.

| | JSON | protobuf (nanopb) |
|---|---|---|
| Wire size (this reading) | ~40 B | ~15 B |
| Names on wire | every message | never (tags only) |
| Add a field | ad-hoc parsing | tag N, back-compatible |
| Heap on ESP32 | via String/ArduinoJson | none (static) |
| Debuggability | reads in a terminal | needs the `.proto` |

On the backend, decode with the same `.proto` compiled for Go/Python/Rust — one schema, every language. Keep the `.proto` in a shared repo and version the tags, never renumber them.

**Try next:** point `mosquitto_sub -t 'fleet/#' -v` at your broker, publish one reading, and confirm the payload is ~15 raw bytes; then add a `uint32 battery_mv = 6;` field, reflash one node, and watch old and new firmware coexist without the backend changing.
