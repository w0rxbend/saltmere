---
title: "Driving the Sensirion SPS30 particulate sensor over I2C on an ESP32"
date: 2026-08-07
track: iot-embedded
summary: "The SPS30 is a laser-scattering PM1.0/2.5/4/10 sensor speaking the Sensirion I2C dialect, with two constraints: a 100 kHz ceiling with no clock stretching, and readings delivered as big-endian IEEE-754 floats split across two CRC-guarded I2C words. The protocol, a correct ESP32 decode, and why the fan-cleaning cycle needs external scheduling."
reading_time: 7
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

**Gist.** Optical particulate sensing requires a host that can command a laser-scattering
measurement chamber and retrieve averaged mass and number concentrations without corrupting
them in transit. The SPS30 solves this with a polled inter-integrated-circuit (I2C) protocol:
16-bit command codes, a cyclic-redundancy-check (CRC) byte after every two data bytes, and
IEEE-754 single-precision floats reassembled from consecutive word pairs. The cost is that the
device imposes a **100 kbit/s bus ceiling with no clock stretching**, so the host — not the
sensor — is responsible for the delay between command and read, and for validating 20 CRC bytes
per sample.

## Measured quantities

The SPS30 reports two independent families of values. **Mass concentrations** in µg/m³ for
PM1.0, PM2.5, PM4 and PM10 — the quantities compared against an air-quality index — and
**number concentrations** in particles per cm³ (#/cm³) for PM0.5, PM1.0, PM2.5, PM4 and PM10.
Number concentration responds to a source (a candle, cooking, a 3D printer) before mass
accumulates, because a burst of sub-micron particles contributes many counts and little mass.
A **typical particle size** in µm is emitted as a tenth value.

The optically resolved size range is **0.3–10 µm**; the PM2.5 bin covers particles from 0.3 to
2.5 µm. Mass concentration range is **0–1000 µg/m³**, number concentration **0–3000 #/cm³**.
Mass-concentration accuracy for PM1.0 and PM2.5 is **±10 µg/m³ over 0–100 µg/m³** and ±10% over
100–1000 µg/m³; the PM4 and PM10 figures are looser. A fresh averaged value
appears approximately once per second while measurement is running; the fan and optics require
several seconds after start before readings are trustworthy.

## The I2C interface and its two constraints

The address is a fixed **7-bit `0x69`**. Every command is a 16-bit code transmitted
most-significant byte first.

**Constraint one: 100 kHz, no clock stretching.** The SPS30 is a standard-mode I2C device with a
maximum bus rate of **100 kbit/s**. Raising the bus to 400 kHz — tolerated by the SEN5x and
SCD4x parts — is outside specification here. The device also **does not use clock stretching**,
the mechanism by which a peripheral holds the clock line low to delay the controller until data
is ready. Without it there is no in-band back-pressure: the transaction must be split into a
write of the command, a host-side wait, and a separate read. A combined write-then-repeated-start
read returns whatever the internal buffer held at that instant, with **no protocol-level error** —
the CRC is computed over whichever bytes are shifted out, so stale data passes the
checksum.

**Constraint two: each reading spans two words.** In the default output format each value is an
IEEE-754 single-precision float, four bytes, but the Sensirion wire format inserts a CRC after
*every two data bytes*. One float therefore arrives as **two I2C words**:
`[b31..b24, b23..b16, CRC][b15..b8, b7..b0, CRC]`. Reconstruction takes the two data bytes from
each of two consecutive words, concatenates them big-endian into a 32-bit quantity, and
reinterprets those bits as a float. **A one-word misalignment is not detected by any CRC** —
each word is individually valid — and produces a denormal or absurd magnitude such as `1.4e-41`
rather than an error.

The command set required for a polling loop:

| Command | Code | Notes |
|---|---|---|
| Start Measurement | `0x0010` | followed by a data word: `0x0300` = float output, `0x0500` = uint16 |
| Read Data-Ready Flag | `0x0202` | returns one word; `0x0001` = new sample ready |
| Read Measured Values | `0x0300` | 60 bytes in float format (10 floats), 30 bytes in uint16 format |
| Stop Measurement | `0x0104` | back to idle |
| Sleep / Wake-up | `0x1001` / `0x1103` | low-power idle; wake needs a dummy address byte first |
| Start Fan Cleaning | `0x5607` | spins the fan to maximum for ~10 s |
| Read/Write Auto-Clean Interval | `0x8004` | 32-bit seconds; default `604800` (1 week) |

## CRC-8 over each word

Every 2-byte word is followed by a one-byte checksum computed over those two data bytes with the
standard Sensirion parameters: **polynomial `0x31` (x⁸ + x⁵ + x⁴ + 1), initial value `0xFF`, no
input or output reflection, no final XOR**. The routine is identical to the one used by the SEN5x
and SCD41 parts and can be reused verbatim.

The scope of the check is what matters. In float format `Read Measured Values` returns 60 bytes —
**20 words, hence 20 CRC bytes**. Each guards two bytes only; it detects bit corruption within a
word, and does not detect a word delivered out of order, a truncated transfer that still yields
whole words, or the stale-buffer case above. Omitting the checks converts a marginal pull-up
resistor into a plausible-looking µg/m³ excursion indistinguishable from a real event in a
months-long log.

## An ESP32 decode

Plain Arduino `Wire`, no vendor library, so the protocol remains visible. The fixed 100 kHz clock
and the two-word float reassembly are the load-bearing parts.

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
    Wire.endTransmission();                      // STOP, not a repeated start
}

void startMeasurement() {                       // 0x0010 + arg word 0x0300 (float)
    Wire.beginTransmission(SPS30);
    Wire.write(0x00); Wire.write(0x10);
    Wire.write(0x03); Wire.write(0x00);         // 0x03 = IEEE754 float format
    Wire.write(crc8(0x03, 0x00));               // CRC over the argument word only
    Wire.endTransmission();
}

bool readWords(uint16_t *out, int n) {          // n words, every CRC checked
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
    Wire.setClock(100000);                      // 100 kHz max, no clock stretching
    startMeasurement();
    delay(8000);                                // fan and optics settle over seconds
}

void loop() {
    cmd(0x0202); delay(5);                       // data-ready flag; delay replaces stretching
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

Words 8–17 hold the five number concentrations (PM0.5, PM1.0, PM2.5, PM4, PM10 in #/cm³) in the
same two-word float layout. **The full 60-byte block must be read even when only mass is wanted**,
since word indices are fixed positions within one transfer.

On ESP-IDF the shape is identical: an `i2c_master_transmit` carrying the command, a
`vTaskDelay`, then an `i2c_master_receive` of 60 bytes. The absence of clock stretching means
that delay is a functional requirement, not a courtesy.

Where a platform's I2C buffer or floating-point support is constrained — classic AVR being the
common case — `0x0500` selects the 30-byte **uint16** block, in which each PM value is an
unsigned 16-bit integer in µg/m³. The ESP32 has both the RAM for a 60-byte transfer and a
hardware floating-point unit, so the float format costs nothing there.

## Fan cleaning and the uptime counter

The SPS30 runs an automatic fan-cleaning cycle every `604800` seconds — one week — **counted in
continuous run time**. The counter does not persist across a stop, a sleep command, or a power
cycle. A logger that deep-sleeps between samples or powers down overnight therefore accumulates
run time in fragments, and if no fragment reaches a week the automatic clean never fires.
Sensirion documents the cleaning cycle as the means of preserving long-term measurement
stability; without it, dust deposited on the optics **drifts the reported concentrations**, and
no error flag is attached to that drift.

The remedy is host-side scheduling: track wall-clock elapsed time independently and issue
**Start Fan Cleaning (`0x5607`)** on a weekly cadence. The cycle spins the fan to maximum for
approximately 10 s. Readings taken while flow is re-stabilising afterwards are not comparable
with the surrounding series and are best discarded.

## Pitfalls

- **`Wire.setClock(400000)` carried over from an SEN5x or SCD4x project.** The SPS30 is
  standard-mode only at 100 kbit/s; exceeding it puts the bus outside the device specification.
- **Command and read merged into one transaction with a repeated start.** With no clock
  stretching the device cannot delay the controller, so the read returns buffer contents from
  before the command was processed — and those bytes carry valid CRCs.
- **Float reassembled from the wrong word pair.** Every individual word passes its CRC, so the
  only symptom is a value such as `1.4e-41` or a magnitude far outside 0–1000 µg/m³.
- **CRC bytes skipped to save cycles.** A marginal pull-up or a long cable produces corrupted
  words that decode into plausible concentrations, indistinguishable from real excursions after
  the fact.
- **Reading fewer than 60 bytes in float format.** Word offsets are positions in a single
  transfer; a short read shifts every subsequent field.
- **Relying on the automatic fan clean on a duty-cycled logger.** The interval counts continuous
  run time only, so a sensor that sleeps nightly may never reach the threshold, and the resulting
  optical fouling drifts readings without raising any flag.
- **Reading immediately after Start Measurement.** The fan and optics need seconds to settle;
  early samples are not representative and carry no indication of that.
- **Wake-up issued without the preceding dummy address byte.** The device remains in sleep and
  the subsequent command is not acknowledged.
