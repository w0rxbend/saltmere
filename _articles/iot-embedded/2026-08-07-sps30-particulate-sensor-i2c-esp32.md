---
title: "Driving the Sensirion SPS30 particulate sensor over I2C on an ESP32"
date: 2026-08-07
track: iot-embedded
summary: "The SPS30 is a laser-scattering PM1.0/2.5/4/10 sensor that speaks the familiar Sensirion I2C dialect — but with two twists: it caps out at 100 kHz with no clock stretching, and each reading is a big-endian IEEE-754 float spread across two CRC-guarded I2C words. Here is the protocol, a correct ESP32 decode, and why you should schedule a weekly fan clean."
reading_time: 6
tags: [sps30, sensirion, air-quality, i2c, esp32, particulate-matter]
sources:
  - title: "Datasheet SPS30 (Version 2.0, June 2023) — Sensirion"
    url: "https://sensirion.com/media/documents/8600FF88/64A3B8D6/Sensirion_PM_Sensors_Datasheet_SPS30.pdf"
  - title: "Sensirion embedded-sps — sps30-i2c/sps30.c reference driver"
    url: "https://github.com/Sensirion/embedded-sps/blob/master/sps30-i2c/sps30.c"
  - title: "Sensirion arduino-i2c-sps30 library"
    url: "https://github.com/Sensirion/arduino-i2c-sps30"
  - title: "RIOT-OS SPS30 driver documentation"
    url: "https://doc.riot-os.org/group__drivers__sps30.html"
  - title: "I2C — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/i2c.html"
---

The SPS30 is Sensirion's standalone particulate-matter sensor: a laser diode fires across a small airflow, particles scatter light onto a photodetector, and an on-board DSP turns the scattering signal into mass concentrations for four size bins. Unlike the Plantower PMS5003 — which streams a fixed frame over UART whether you asked for it or not — the SPS30 is a proper I2C peripheral you command and poll. It also exposes far more: number concentrations and a typical-particle-size estimate the PMS5003 never reports. If you have already wired a SEN5x or an SCD41, most of this will feel familiar, because the SPS30 shares the same 16-bit-command, CRC-per-word protocol. But it has two sharp edges that catch people, and both are worth understanding before you trust a single reading.

## What it actually measures

The SPS30 reports two independent families of values. **Mass concentrations** in µg/m³ for PM1.0, PM2.5, PM4, and PM10 — the numbers you compare against an air-quality index. And **number concentrations** in #/cm³ for PM0.5, PM1.0, PM2.5, PM4, and PM10 — raw particle counts per size bin, useful for spotting a source (a candle, cooking, a 3D printer) before mass climbs. It also emits a **typical particle size** in µm.

The optically resolved size range is 0.3–10 µm; the PM2.5 bin, for instance, is "particles from 0.3 to 2.5 µm." Mass concentration range is 0–1000 µg/m³ and number concentration 0–3000 #/cm³. Accuracy for PM1/PM2.5 is ±(5 µg/m³ + 5%) from 0–100 µg/m³. It provides a fresh averaged value roughly once per second while running, though the fan and optics need several seconds after start to settle before readings are trustworthy.

## The I2C interface, and its two twists

Fixed **7-bit address `0x69`**. Every command is a 16-bit code sent MSB-first. So far, standard Sensirion.

**Twist one: 100 kHz, and no clock stretching.** The SPS30 is a standard-mode I2C device — maximum **100 kbit/s**. Do not call `Wire.setClock(400000)` out of habit; the SEN5x and SCD4x tolerate 400 kHz, the SPS30 does not. And critically, it **does not use clock stretching**, so you cannot lean on the sensor to hold the clock while it prepares data. Instead you must respect the execution-time gap yourself: write the command, wait, then issue a separate read. Merge them and you read stale or garbage bytes with no protocol-level warning.

**Twist two: readings are big-endian floats spanning two words.** In its default output format each value is an IEEE-754 float — four bytes — but Sensirion's wire format inserts a CRC after *every two data bytes*. So one float arrives as **two I2C words**: `[MSB, MSB-1, CRC][MSB-2, LSB, CRC]`. To rebuild a float you take the two data bytes from each of two consecutive words, concatenate them big-endian, and reinterpret the 32 bits. Get the word boundaries wrong and PM2.5 comes out as `1.4e-41` or similar nonsense.

The commands you need:

| Command | Code | Notes |
|---|---|---|
| Start Measurement | `0x0010` | followed by a data word: `0x0300` = float output, `0x0500` = uint16 |
| Read Data-Ready Flag | `0x0202` | returns one word; `0x0001` = new sample ready |
| Read Measured Values | `0x0300` | 60 bytes in float format (10 floats), 30 bytes in uint16 format |
| Stop Measurement | `0x0104` | back to idle |
| Sleep / Wake-up | `0x1001` / `0x1103` | low-power idle; wake needs a dummy address byte first |
| Start Fan Cleaning | `0x5607` | spins the fan to max for ~10 s |
| Read/Write Auto-Clean Interval | `0x8004` | 32-bit seconds; default `604800` (1 week) |

## CRC-8: the same family, verify it

Every 2-byte word is followed by a one-byte checksum, and it is the standard Sensirion CRC — **polynomial `0x31` (x⁸ + x⁵ + x⁴ + 1), init `0xFF`, no reflection, no final XOR**, computed over the two data bytes. If you have read the SEN5x or SCD41 pieces this is the identical routine; reuse it verbatim. In float format `Read Measured Values` returns 60 bytes — that is 20 words, so **20 CRC bytes to check**. Skip them and a marginal pull-up turns into a plausible-but-wrong µg/m³ spike buried in months of logs.

## A correct ESP32 decode

Plain Arduino `Wire`, no vendor library, so the protocol is fully visible. Note the fixed 100 kHz clock and the two-word float reassembly.

```cpp
#include <Wire.h>
const uint8_t SPS30 = 0x69;

uint8_t crc8(uint8_t msb, uint8_t lsb) {
    uint8_t d[2] = {msb, lsb}, crc = 0xFF;      // init 0xFF
    for (int i = 0; i < 2; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);  // poly 0x31
    }
    return crc;
}

void cmd(uint16_t c) {                          // pointer-only command
    Wire.beginTransmission(SPS30);
    Wire.write(c >> 8); Wire.write(c & 0xFF);
    Wire.endTransmission();
}

void startMeasurement() {                       // 0x0010 + arg word 0x0300 (float)
    Wire.beginTransmission(SPS30);
    Wire.write(0x00); Wire.write(0x10);
    Wire.write(0x03); Wire.write(0x00);         // 0x03 = IEEE754 float format
    Wire.write(crc8(0x03, 0x00));               // CRC over the 2 data bytes
    Wire.endTransmission();
}

bool readWords(uint16_t *out, int n) {          // n words, CRC-checked
    if (Wire.requestFrom(SPS30, n * 3) != n * 3) return false;
    for (int i = 0; i < n; i++) {
        uint8_t msb = Wire.read(), lsb = Wire.read(), crc = Wire.read();
        if (crc8(msb, lsb) != crc) return false;
        out[i] = (msb << 8) | lsb;
    }
    return true;
}

float wordsToFloat(uint16_t hi, uint16_t lo) {  // big-endian across two words
    uint32_t bits = ((uint32_t)hi << 16) | lo;
    float f; memcpy(&f, &bits, 4); return f;
}

void setup() {
    Serial.begin(115200);
    Wire.begin();
    Wire.setClock(100000);                      // SPS30 is 100 kHz max, no stretching
    startMeasurement();
    delay(8000);                                // fan + optics need seconds to settle
}

void loop() {
    cmd(0x0202); delay(5);                       // read data-ready flag
    uint16_t ready;
    if (!readWords(&ready, 1) || ready != 0x0001) { delay(200); return; }

    cmd(0x0300); delay(5);                        // read measured values (float)
    uint16_t w[20];                              // 10 floats = 20 words = 60 bytes
    if (!readWords(w, 20)) { Serial.println("CRC error"); return; }

    float pm1_0 = wordsToFloat(w[0],  w[1]);
    float pm2_5 = wordsToFloat(w[2],  w[3]);
    float pm4_0 = wordsToFloat(w[4],  w[5]);
    float pm10  = wordsToFloat(w[6],  w[7]);
    float typ_um = wordsToFloat(w[18], w[19]);   // typical particle size
    Serial.printf("PM1=%.1f PM2.5=%.1f PM4=%.1f PM10=%.1f ug/m3  size=%.2fum\n",
                  pm1_0, pm2_5, pm4_0, pm10, typ_um);
    delay(1000);
}
```

Words 8–17 hold the five number concentrations (PM0.5, PM1.0, PM2.5, PM4, PM10 in #/cm³) in the same two-word float layout if you want them. On ESP-IDF the shape is identical: an `i2c_master_transmit` for the command, a small `vTaskDelay`, then an `i2c_master_receive` of 60 bytes — the SPS30's lack of clock stretching means the explicit delay is doing real work, not just being polite.

If your platform's I2C buffer or float handling is awkward — classic AVR is the usual culprit — send `0x0500` instead and read the 30-byte **uint16** block, where each PM value is a plain `uint16` scaled to µg/m³. On an ESP32 you have the RAM and the FPU, so prefer float.

## Schedule the fan clean yourself

The SPS30 auto-runs a fan-cleaning cycle every `604800` seconds — one week — **but only counting continuous run time**. If your logger sleeps at night, deep-sleeps between samples, or power-cycles daily, that counter keeps resetting and the automatic clean may never fire. Dust on the optics slowly biases readings low. The fix is a five-second job: track your own uptime, and once a week issue **Start Fan Cleaning (`0x5607`)**, which spins the fan to full for ~10 s to blow the chamber clear. Do it at a fixed off-hour and discard readings for the following minute while flow re-stabilizes.

## Takeaways

- Address `0x69`, **100 kHz max, no clock stretching** — respect the write-wait-read gap manually.
- Start with `0x0010` + `0x0300` for float output, poll `0x0202`, then read 60 bytes from `0x0300`.
- Each float spans **two CRC-guarded words**; reassemble big-endian and check all 20 CRCs (poly `0x31`, init `0xFF`).
- The weekly auto-clean counts uptime only — trigger `0x5607` yourself if the sensor ever sleeps.

**Try next:** Log number concentration alongside mass for an evening, then light a candle across the room. Watch the PM0.5 #/cm³ count jump seconds before PM2.5 mass moves — that lead time is exactly what the SPS30 gives you over a mass-only UART sensor like the PMS5003.
