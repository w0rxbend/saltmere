---
title: "Driving a SEN5x over raw I2C: commands, CRC-8, and data-ready polling"
date: 2026-07-30
track: iot-embedded
summary: "Sensirion's SEN5x air-quality node can be driven without the vendor library: 16-bit commands, a CRC-8 with polynomial 0x31 and initialisation 0xFF after every data word, and a wait between command and read. The same checksum is shared by other Sensirion parts. Protocol description plus a dependency-free ESP32 sketch."
reading_time: 7
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

**Gist.** The SEN5x environmental node exposes particulate, humidity, temperature and, on the larger members of the family, volatile organic compound (VOC) and nitrogen oxide (NOx) indices over Inter-Integrated Circuit (I2C), and a vendor library is not required to read it. The protocol is a small set of **16-bit commands** followed, for every two data bytes returned, by a **CRC-8 checksum with polynomial `0x31` and initialisation `0xFF`**. The cost is that the host must respect the separation between command and read, must implement that specific checksum rather than a generic CRC-8, and must sign-extend the words the datasheet marks as signed.

The SEN5x family (SEN50, SEN54, SEN55) reports particulate matter at 1.0, 2.5, 4.0 and 10 µm size cuts (PM1.0/PM2.5/PM4.0/PM10); the SEN54 adds relative humidity, temperature and a VOC index, and the SEN55 adds an NOx index on top of those. The checksum described here is shared with other Sensirion I2C parts including the SCD4x, SHT4x and SGP4x, so the verification code written once transfers to those parts; the command encoding does not transfer unchanged, since the SHT4x uses single-byte commands rather than the 16-bit commands of the SEN5x.

## The command set

Every command is a **16-bit value sent most-significant byte first** to the fixed I2C address **`0x69`**; the SEN5x does not expose an address-select mechanism. Four commands are sufficient for continuous measurement:

| Command | Hex | Notes |
|---------|-----|-------|
| Start Measurement | `0x0021` | begins sampling; the datasheet gives a 50 ms execution time and a 1 s measurement interval, so the first sample is not available immediately |
| Read Data-Ready Flag | `0x0202` | returns 1 word: nonzero means a new sample is available |
| Read Measured Values | `0x03C4` | returns 8 words (PM×4, RH, T, VOC, NOx) |
| Stop Measurement | `0x0104` | returns the device to idle |

A read is **two transactions, not one**: write the command, wait the execution time the datasheet gives for that command, then issue a **repeated-start read** of the expected byte count. Collapsing the pair into a single transaction removes the interval the sensor uses to prepare the response.

Two invariants govern the exchange. First, **the byte count of a read is exactly three bytes per word** — two data bytes and one checksum — so a host that requests `2n` bytes for `n` words desynchronises immediately and every subsequent word is misaligned. Second, **the data-ready flag is the only sanction for reading measured values**; sampling is periodic and the host loop is not, so an unconditional read of `0x03C4` returns either a repeat of the previous sample or an incomplete one.

## The checksum

Each 16-bit word on the wire is followed by a one-byte checksum, and it is not the CRC-8 configuration a general-purpose library defaults to. The parameters are:

- **Polynomial `0x31`** (x⁸ + x⁵ + x⁴ + 1)
- **Initialisation `0xFF`**
- **No input or output reflection, no final XOR**
- Computed over the **two data bytes of that word only** — the checksum is per word, not per message

Every parameter participates in the result, so a host that gets one of them wrong disagrees with the sensor on most words rather than on a rare few; the symptom is a driver that reports checksum failures continuously, not intermittently.

```cpp
uint8_t sensirion_crc(uint8_t msb, uint8_t lsb) {
    uint8_t data[2] = {msb, lsb};
    uint8_t crc = 0xFF;                 // initialisation
    for (int i = 0; i < 2; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x31   // polynomial
                               : (crc << 1);
    }
    return crc;
}
```

The per-word placement has a direct consequence for error handling: a corrupted word is detectable in isolation, so a host can reject one measurement without discarding the whole frame, and can retry the read rather than propagate a plausible-looking but wrong value into a log.

## Decoding: signedness and scale

`Read Measured Values` returns eight 16-bit words, each with its own checksum. The scale factors are fixed, and the humidity, temperature, VOC and NOx words are **signed**. Reading a signed word as unsigned is the failure that survives testing longest, because it is invisible while all quantities are positive: **a temperature below 0 °C has the top bit set and, interpreted as unsigned, decodes as a large positive value** instead of a small negative one.

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

The division must be performed in floating point or with the scale applied after the signed cast; casting after dividing reintroduces the same defect.

## A dependency-free ESP32 sketch

The sketch below uses the Arduino `Wire` interface only. The poll on the data-ready flag is the part that removes the two most common symptoms — garbage on the first read, and repeated identical samples — by waiting for the sensor's own signal rather than assuming a fixed cadence.

```cpp
#include <Wire.h>
const uint8_t SEN5X = 0x69;

void cmd(uint16_t c) {
    Wire.beginTransmission(SEN5X);
    Wire.write(c >> 8); Wire.write(c & 0xFF);
    Wire.endTransmission();
}

bool readWords(uint16_t *out, int n) {          // n words, each 2 bytes + CRC
    Wire.requestFrom(SEN5X, (uint8_t)(n * 3));  // three bytes per word
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
    delay(1000);                // warm-up before the first read
}

void loop() {
    cmd(0x0202); delay(20);                     // read data-ready flag
    uint16_t ready;
    if (!readWords(&ready, 1) || ready == 0) { delay(100); return; }

    cmd(0x03C4); delay(20);                     // read measured values
    uint16_t v[8];
    if (!readWords(v, 8)) { Serial.println("CRC error"); return; }

    float pm25 = v[1] / 10.0f;
    float rh   = (int16_t)v[4] / 100.0f;        // signed
    float tC   = (int16_t)v[5] / 200.0f;        // signed
    int   voc  = (int16_t)v[6] / 10;
    Serial.printf("PM2.5=%.1f ug/m3  RH=%.1f%%  T=%.2fC  VOC=%d\n", pm25, rh, tC, voc);
    delay(1000);
}
```

Every word passes the checksum before it is used, so a marginal pull-up resistor or an over-long bus surfaces as a rejected read rather than as a value that is wrong but within range. For a logger that runs unattended for months, that is the difference between a visible fault and a silently corrupted series.

A useful extension is the `0xD014` Read Product Name command, which returns up to 32 American Standard Code for Information Interchange (ASCII) bytes, still with one checksum per word, and allows the host to confirm the part before trusting its readings. Wrapping `readWords` in a bounded retry turns a transient bus disturbance into a delayed sample rather than a discarded one.

## Pitfalls

- **Requesting two bytes per word instead of three.** The checksum bytes are read as data, every word after the first is shifted by one byte, and the values are wrong without any checksum failure being reported — because the checksums are never compared.
- **Using a library's default CRC-8.** A different polynomial, an initialisation of `0x00`, or input reflection makes practically every word fail verification; the symptom is a driver that reports constant checksum errors on a perfectly good bus.
- **Reading the temperature and humidity words as unsigned.** Below 0 °C, or at any point where the sign bit is set, the value decodes as a large positive number rather than a negative one, and the defect is invisible in warm-weather testing.
- **Merging the command write and the data read into one transaction.** The sensor is not given the execution time between the two, and the read returns data that is stale or incomplete.
- **Reading `0x03C4` without checking `0x0202` first.** Sampling is periodic while the host loop is not, so consecutive reads can return the same sample, and reads issued too soon after `0x0021` return data before the first measurement completes.
- **Assuming the I2C address is configurable.** The SEN5x address is fixed at `0x69`, so two SEN5x parts cannot share one bus segment without a multiplexer or a second bus.
