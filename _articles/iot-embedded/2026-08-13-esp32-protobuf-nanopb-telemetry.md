---
title: "Shrinking ESP32 telemetry with Protocol Buffers and nanopb"
date: 2026-08-13
track: iot-embedded
summary: "Field names dominate a small JSON telemetry frame. Protocol Buffers replaces them with integer tags and varints, and nanopb encodes the result on an ESP32 with static allocation and a few kilobytes of flash. This walks the .proto, the generated C, the wire-size derivation, and the MQTT publish path."
reading_time: 7
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

**Gist.** A JSON air-quality frame such as `{"pm25":12.4,"co2":812,"ts":1723560000}` occupies roughly 40 bytes, of which the field names and punctuation are a large fraction and are retransmitted on every message; over a metered cellular link or a duty-cycle-limited LoRaWAN uplink that overhead is the dominant cost. Protocol Buffers removes the names from the wire entirely — **fields are identified by an integer tag, and integers are length-varying (varint) encoded** — and nanopb implements the encoder for microcontrollers with static allocation, no dynamic memory, and a few kilobytes of code. The cost is that the payload is no longer self-describing: **both ends must hold the same `.proto` schema**, and a byte stream cannot be read without it.

## Where the bytes go

Protocol Buffers encodes each present field as a **key byte followed by a value**. The key is `(field_number << 3) | wire_type`, itself a varint, so **field numbers 1 through 15 cost a single key byte** while 16 and above cost two. The wire type selects how the value is read: varint (type 0) for `uint32`, `int32`, `sint32` and `bool`; fixed 32-bit (type 5) for `float` and `fixed32`; length-delimited (type 2) for strings, bytes and nested messages.

A varint stores seven payload bits per byte, using the top bit as a continuation flag. Small magnitudes are therefore cheap and large ones are not: a value below 128 occupies one byte, one below 16 384 occupies two, and a Unix-seconds timestamp such as `1723560000` — which exceeds 2^28 — occupies five.

That is enough to derive the size of the three-field reading rather than assert it:

- `ts` — key 1 byte, varint 5 bytes → **6 bytes**
- `pm25` — key 1 byte, fixed 32-bit float 4 bytes → **5 bytes**
- `co2` = 812 — key 1 byte, varint 2 bytes → **3 bytes**

**14 bytes total**, against roughly 40 for the JSON form. The saving is not a fixed ratio; it scales with how verbose the field names are and how small the numbers are.

Signed values need care. In the proto3 encoding a negative `int32` is sign-extended to 64 bits before varint encoding, so **any negative `int32` always occupies ten bytes**. The `sint32` type instead applies zigzag mapping, `n → (n << 1) XOR (n >> 31)`, which interleaves negatives with positives so that −1 becomes 1 and −2 becomes 3. Small magnitudes of either sign then stay in one or two bytes.

## The schema

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

Temperature is scaled to deci-degrees and carried as `sint32` rather than as a `float`: a float is unconditionally five bytes on the wire including its key, whereas a zigzagged deci-degree reading in the range ±6 400 fits in a two-byte varint plus its key.

**Schema evolution rests on the tag numbers, not the names.** Adding `uint32 battery_mv = 6;` is backward compatible in both directions: an old decoder encounters an unrecognised field number and skips it using the wire type embedded in the key, and a new decoder reading an old message finds field 6 absent. Renaming a field changes nothing on the wire. **Renumbering or reusing a tag is the operation that silently corrupts data**, because a decoder will interpret the old bytes under the new field's meaning whenever the wire types happen to agree.

The corresponding proto3 rule is that **a scalar field equal to its default — zero, `false`, the empty string — is not emitted at all**. Absence and zero are indistinguishable after decoding. A `co2` of 0 ppm and a `co2` that was never set both arrive as 0.

## Generating C with nanopb

nanopb (**0.4** series) ships a `protoc` plugin. It reads an optional `.options` file alongside the `.proto`, whose purpose is to **bind every variable-length field to a compile-time maximum** so that the generated struct is fixed-size:

```bash
# sensor.options — no malloc, fixed buffers only
# (only needed for string/bytes/repeated fields, e.g.:)
# SensorReading.fw_version  max_size:16

nanopb_generator.py sensor.proto
# -> sensor.pb.h and sensor.pb.c
```

With `max_size` set, a `string fw_version` becomes a `char fw_version[16]` member. Without it the generator emits a pointer or callback field and the caller must supply the storage or a callback, which reintroduces dynamic allocation. **A message whose fields are all scalars needs no `.options` at all**: the generated struct is flat and fixed-size and is safe to place on the stack.

The build needs `sensor.pb.c` plus the three runtime translation units `pb_encode.c`, `pb_decode.c` and `pb_common.c`. A firmware that only uplinks can omit `pb_decode.c`.

## Encoding and publishing over MQTT

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
  // os.bytes_written is the exact payload length — protobuf is not fixed-width
  return mqtt.publish("fleet/node-kitchen-01/tele", buf, os.bytes_written, false);
}
```

`SensorReading_init_zero` is the generated initialiser that clears the struct; omitting it leaves stack garbage in unassigned members, which proto3 will then encode as though the values were deliberate. `pb_ostream_from_buffer` wraps a caller-owned buffer, so **the encoder performs no allocation and cannot exceed the array**: on overflow `pb_encode` returns `false` and `PB_GET_ERROR` yields a diagnostic string, leaving the buffer partially written and unusable.

Because the encoded length varies with the values, `os.bytes_written` — not `sizeof(buf)` — is the payload length. The three-argument `PubSubClient::publish` overload taking a `const uint8_t*` and a length is the one to use; the `const char*` overload stops at the first zero byte, and **a protobuf payload contains zero bytes routinely** (any varint byte or float byte may be 0x00). The final argument is the retain flag, left `false` so the broker does not replay a stale reading to new subscribers.

Where the buffer size must be chosen rather than guessed, nanopb generates a `SensorReading_size` constant giving the worst-case encoded length for messages with no unbounded fields, and `pb_get_encoded_size` computes the exact length of a specific message without writing it.

| | JSON | protobuf (nanopb) |
|---|---|---|
| Wire size (three-field reading) | ~40 B | 14 B |
| Names on wire | every message | never (tags only) |
| Adding a field | ad-hoc parsing on both ends | new tag, compatible both ways |
| Heap on ESP32 | via `String`/ArduinoJson | none with fixed-size fields |
| Readability | reads in a terminal | requires the `.proto` |

On the ingest side the same `.proto` compiles for Go, Python or Rust, so one schema definition serves every consumer. Keeping that file in a shared repository, and treating tag numbers as immutable once deployed to a device that may never be reflashed, is what makes the compatibility guarantee real.

## Pitfalls

- **A zero-valued field vanishes.** proto3 omits scalars equal to their default, so a genuine `co2` of 0 and an unset `co2` decode identically. Distinguishing them requires an explicit presence marker, such as `optional` or a separate validity field.
- **Reusing a retired tag number corrupts history.** A decoder matches on the integer only; if the old and new fields share a wire type, stale messages decode without error into the wrong field. Reserve retired numbers instead.
- **`int32` for a quantity that goes negative costs ten bytes.** Negative `int32` values are sign-extended to 64 bits before varint encoding. `sint32` with zigzag mapping keeps small negative magnitudes small.
- **Publishing the payload as a C string truncates it.** Protobuf output contains 0x00 bytes as ordinary data; the `const char*` publish overload treats the first one as the end of the message.
- **Sizing the buffer from `sizeof(buf)` sends trailing garbage.** The encoded length is `os.bytes_written`; the remainder of the stack buffer is uninitialised.
- **Omitting `_init_zero` encodes stack garbage.** Unassigned members hold whatever was previously on the stack, and any non-default value is emitted as if it were a real reading.
- **String or bytes fields without a `max_size` in `.options` are not statically allocated.** The generator falls back to pointer or callback fields, and the "no heap" property is lost without a compile error to signal it.
- **A message larger than the client's buffer fails at publish, not at encode.** PubSubClient enforces a maximum packet size and returns `false`; the encode step succeeded, so the failure surfaces only in the return value.
