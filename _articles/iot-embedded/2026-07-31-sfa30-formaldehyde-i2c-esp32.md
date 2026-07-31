---
title: "Reading Formaldehyde with the Sensirion SFA30 over I2C on an ESP32"
date: 2026-07-31
track: iot-embedded
summary: "The SFA30 is an electrochemical formaldehyde sensor that speaks plain I2C. Here is the address, the four commands you actually need, the CRC-8 that trips everyone up, and working ESP32 code."
reading_time: 6
tags: [sfa30, formaldehyde, hcho, i2c, esp32, air-quality, sensirion]
sources:
  - title: "Sensirion SFA30 Datasheet (D1 V1.3, January 2025)"
    url: "https://sensirion.com/media/documents/DEB1C6D6/6789009D/GAS_DS_SFA30_D1.pdf"
  - title: "Sensirion Formaldehyde Sensor I2C Interface Description"
    url: "https://sensirion.com/media/documents/974A16B3/634D0684/Sensirion_Formaldehyde_Sensor_I2C_Interface.pdf"
  - title: "Sensirion embedded-i2c-sfa3x driver (GitHub)"
    url: "https://github.com/Sensirion/embedded-i2c-sfa3x"
  - title: "ESPHome SFA30 component"
    url: "https://esphome.io/components/sensor/sfa30/"
---

## Why formaldehyde needs its own sensor

Most "air quality" boards ship with a metal-oxide (MOX) VOC sensor that reports a total VOC index or a computed eCO2. Those are useful for "is the room stuffy," but they blur every gas into one number and drift with humidity. Formaldehyde (HCHO) is a specific, regulated indoor pollutant: it off-gasses for months from particleboard furniture, laminate flooring, adhesives, and new textiles, and the WHO guideline sits at roughly 80 ppb over 30 minutes. A total-VOC index can't tell you whether that reading is your new desk or the cleaning spray you just used.

The Sensirion SFA30 solves this with an electrochemical cell tuned for HCHO with low cross-sensitivity to ethanol (the classic false-positive gas). It bundles an SHT humidity/temperature sensor on the same module, and it speaks I2C, UART, or SHDLC. This article covers the I2C path on an ESP32.

## The interface in four commands

The default I2C address is **0x5D** (7-bit). Every command is a 16-bit code sent MSB first. You only need four:

| Command | Code | Notes |
|---|---|---|
| Start continuous measurement | `0x0006` | Send once; sensor then samples ~2 Hz |
| Read measured values | `0x0327` | Returns 9 bytes: 3 words + CRC each |
| Get device marking | `0xD060` | 48-byte ASCII string (32 chars + CRCs) |
| Device reset | `0xD304` | Soft reset, same as power cycle |

After issuing a command, wait at least **5 ms** before reading the response. Values come back as **signed** 16-bit integers, big-endian, each 2-byte word followed by one CRC byte:

- **HCHO** = raw / 5 → ppb (range 0–1000 ppb, accuracy ±20 ppb or ±20%)
- **Humidity** = raw / 100 → %RH
- **Temperature** = raw / 200 → °C

Note the scale factor for formaldehyde is **5**, not 10 — a common copy-paste error. And the read command is `0x0327`, not one of the `0xD0xx` diagnostic codes.

## Warm-up and cadence

Send `0x0006` once at startup. The formaldehyde channel is **suppressed for the first 10 seconds** after power-up — it returns 0 ppb during that window, so don't panic. Humidity and temperature are valid immediately. The sensor updates at ~1.8–2.2 Hz; poll `0x0327` on any interval from 0.5 s up to 60 s. Leave it running continuously if you can — the electrochemical baseline is happiest when it isn't cold-started repeatedly.

## The CRC-8 everyone gets wrong

Each word carries a CRC-8 byte. Getting the parameters wrong means every read looks corrupt. Sensirion uses: **polynomial 0x31, init 0xFF, no input/output reflection, final XOR 0x00**, computed over the two data bytes.

```c
uint8_t sfa30_crc(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc; // no final XOR
}
```

## ESP32 Arduino example

```cpp
#include <Wire.h>

static const uint8_t SFA30_ADDR = 0x5D;

uint8_t sfa30_crc(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
  }
  return crc;
}

void sendCmd(uint16_t cmd) {
  Wire.beginTransmission(SFA30_ADDR);
  Wire.write(cmd >> 8);
  Wire.write(cmd & 0xFF);
  Wire.endTransmission();
}

void setup() {
  Serial.begin(115200);
  Wire.begin();                 // SDA=21, SCL=22 on most ESP32 dev boards
  sendCmd(0x0006);              // start continuous measurement
  delay(10);
}

void loop() {
  sendCmd(0x0327);             // read measured values
  delay(5);                    // required settle time
  uint8_t buf[9];
  Wire.requestFrom(SFA30_ADDR, (uint8_t)9);
  for (int i = 0; i < 9 && Wire.available(); i++) buf[i] = Wire.read();

  // validate CRC on each of the 3 words
  bool ok = true;
  for (int w = 0; w < 3; w++)
    if (sfa30_crc(&buf[w * 3], 2) != buf[w * 3 + 2]) ok = false;

  if (!ok) { Serial.println("CRC error"); delay(1000); return; }

  int16_t hchoRaw = (buf[0] << 8) | buf[1];
  int16_t rhRaw   = (buf[3] << 8) | buf[4];
  int16_t tRaw    = (buf[6] << 8) | buf[7];

  Serial.printf("HCHO: %.1f ppb  RH: %.1f %%  T: %.2f C\n",
                hchoRaw / 5.0, rhRaw / 100.0, tRaw / 200.0);
  delay(1000);
}
```

The three casts to `int16_t` matter: temperature and, in theory, the sensor's internal math are signed, so treating the bytes as unsigned will give nonsense below 0 °C.

## Sanity checks

- Reading all `0xFF` or getting a bus timeout usually means the address is wrong or SDA/SCL are swapped. Confirm 0x5D with an `i2cdetect`-style scan first.
- HCHO stuck at exactly 0 for the first 10 s is expected, not a wiring fault.
- If humidity/temperature look right but HCHO is implausibly high after a solder session, give it minutes — the cell recovers from transient contamination.

**Try next:** Log HCHO alongside a MOX VOC-index sensor (like the SGP40) on the same ESP32 and open a window after unboxing new furniture — watch which curve actually tracks the formaldehyde event versus which one just reacts to everything.
