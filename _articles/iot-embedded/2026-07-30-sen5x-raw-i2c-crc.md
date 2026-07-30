---
title: "Talking to a SEN5x over raw I2C: commands, CRC-8, and clock stretching"
date: 2026-07-30
track: iot-embedded
summary: "You can drive Sensirion's SEN5x air-quality sensor without the vendor library — it's just a handful of 16-bit commands, a specific CRC-8, and some patience for the sensor's warm-up. Doing it by hand teaches you the I2C command/CRC pattern every Sensirion part shares. Here's the protocol and a dependency-free ESP32 sketch."
reading_time: 6
tags: [esp32, sen5x, i2c, air-quality, crc, sensirion]
sources:
  - title: "Datasheet SEN5x — Environmental Sensor Node (Sensirion)"
    url: "https://sensirion.com/media/documents/6791EFA0/62A1F68F/Sensirion_Datasheet_Environmental_Node_SEN5x.pdf"
  - title: "python-i2c-sen5x — Sensirion reference I2C driver"
    url: "https://sensirion.github.io/python-i2c-sen5x/quickstart.html"
  - title: "Writing a Rust Driver for the Sensirion SEN5x — hauju (dev.to)"
    url: "https://dev.to/hauju/writing-a-rust-driver-for-the-sensirion-sen5x-air-quality-sensor-27fb"
  - title: "I2C — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/i2c.html"
---

The SEN5x (SEN54/SEN55) is Sensirion's all-in-one air node: PM1.0/2.5/4/10 particulates, humidity, temperature, and — on the SEN55 — VOC and NOx indices, all over one I2C bus. There's a perfectly good vendor library, but wiring it up yourself is worth an afternoon, because the SEN5x speaks the *same* command/CRC dialect as nearly every modern Sensirion part (SCD4x, SHT4x, SGP4x). Learn it once here and you can talk to any of them from a bare `Wire` interface with no dependencies.

## The protocol in one screen

Everything is a **16-bit command** sent MSB-first to I2C address **`0x69`** (fixed — the SEN5x doesn't let you change it). Commands that carry or return data interleave a **CRC-8 checksum after every two data bytes**. The four you need:

| Command | Hex | Notes |
|---------|-----|-------|
| Start Measurement | `0x0021` | begin sampling; needs ~1 s before first valid data |
| Read Data-Ready Flag | `0x0202` | returns 1 word: nonzero = a new sample is ready |
| Read Measured Values | `0x03C4` | returns 8 words (PM×4, RH, T, VOC, NOx) |
| Stop Measurement | `0x0104` | return to idle |

The read pattern for Sensirion parts is always two transactions: **write the command**, wait the datasheet's execution time, then **issue a repeated-start read** of the expected byte count. Don't merge them — the sensor needs that gap to prepare the data.

## The CRC that trips everyone up

Every Sensirion 16-bit word on the wire is followed by a one-byte checksum, and it is **not** the CRC-8 your library ships by default. The parameters are specific:

- **Polynomial `0x31`** (x⁸ + x⁵ + x⁴ + 1)
- **Initialization `0xFF`**
- No reflection, no final XOR
- Computed over the **two data bytes** of each word

Get any parameter wrong and the bytes look like noise. Here it is, and it's the single most reusable function in your Sensirion toolbox:

```cpp
uint8_t sensirion_crc(uint8_t msb, uint8_t lsb) {
    uint8_t data[2] = {msb, lsb};
    uint8_t crc = 0xFF;                 // init
    for (int i = 0; i < 2; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31   // poly
                               : (crc << 1);
    }
    return crc;
}
```

## Decoding: signed, and scaled

`Read Measured Values` returns eight `uint16` words, each with its CRC. The scale factors are fixed and the temperature/humidity words are **signed** — casting through `int16_t` before scaling is the bug people hit (a below-zero temperature otherwise reads as a huge positive number):

| Word | Quantity | Scale | Type |
|------|----------|-------|------|
| 0 | PM1.0 | ÷ 10 → µg/m³ | unsigned |
| 1 | PM2.5 | ÷ 10 → µg/m³ | unsigned |
| 2 | PM4.0 | ÷ 10 → µg/m³ | unsigned |
| 3 | PM10  | ÷ 10 → µg/m³ | unsigned |
| 4 | Humidity | ÷ 100 → %RH | **signed** |
| 5 | Temperature | ÷ 200 → °C | **signed** |
| 6 | VOC index | ÷ 10 | signed |
| 7 | NOx index | ÷ 10 | signed |

## A dependency-free ESP32 sketch

Plain Arduino `Wire`, no libraries. Note the `while` on the data-ready flag — this is where you'd otherwise hit a **clock-stretching** or "reads garbage" mystery: the SEN5x needs a moment after `0x0021`, and a fresh sample isn't ready every loop, so you poll `0x0202` instead of assuming.

```cpp
#include <Wire.h>
const uint8_t SEN5X = 0x69;

void cmd(uint16_t c) {
    Wire.beginTransmission(SEN5X);
    Wire.write(c >> 8); Wire.write(c & 0xFF);
    Wire.endTransmission();
}

bool readWords(uint16_t *out, int n) {          // reads n words + CRC each
    Wire.requestFrom(SEN5X, n * 3);             // 2 data + 1 CRC per word
    for (int i = 0; i < n; i++) {
        uint8_t msb = Wire.read(), lsb = Wire.read(), crc = Wire.read();
        if (sensirion_crc(msb, lsb) != crc) return false;   // reject bad word
        out[i] = (msb << 8) | lsb;
    }
    return true;
}

void setup() {
    Serial.begin(115200);
    Wire.begin();               // default SDA/SCL
    cmd(0x0021);                // start measurement
    delay(1000);                // datasheet warm-up before first read
}

void loop() {
    cmd(0x0202); delay(20);                     // read data-ready flag
    uint16_t ready;
    if (!readWords(&ready, 1) || ready == 0) { delay(100); return; }

    cmd(0x03C4); delay(20);                     // read measured values
    uint16_t v[8];
    if (!readWords(v, 8)) { Serial.println("CRC error"); return; }

    float pm25 = v[1] / 10.0f;
    float rh   = (int16_t)v[4] / 100.0f;        // signed!
    float tC   = (int16_t)v[5] / 200.0f;        // signed!
    int   voc  = (int16_t)v[6] / 10;
    Serial.printf("PM2.5=%.1f ug/m3  RH=%.1f%%  T=%.2fC  VOC=%d\n", pm25, rh, tC, voc);
    delay(1000);
}
```

Flash it and you get live readings with zero external dependencies. Every word is CRC-checked, so a flaky pull-up or a too-long cable surfaces as a clean "CRC error" instead of a plausible-but-wrong number — which, for an air-quality logger you'll trust for months, is exactly the failure mode you want to be loud.

## Why do it the hard way

Two payoffs. First, portability of understanding: the *command → wait → read-with-CRC* dance and the `0x31`/`0xFF` CRC are common across the Sensirion catalogue, so the SCD41 CO₂ sensor or an SHT4x is now a datasheet lookup, not a new library hunt. Second, robustness: because you own the decode, you control the CRC handling, the signed casts, and the retry policy — the three things wrapper libraries hide and that bite you at the edges (sub-zero temps, marginal wiring, sensors that aren't ready yet).

**Try next:** Add the `0xD014` "Read Product Name" command (returns up to 32 ASCII bytes, still CRC-per-word) to confirm the part before trusting readings, and wrap `readWords` with a two-try retry on CRC failure — then jiggle the I2C wiring and watch it recover instead of logging a bogus PM2.5 spike.
