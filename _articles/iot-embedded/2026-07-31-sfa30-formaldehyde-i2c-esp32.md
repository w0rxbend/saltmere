---
title: "Reading Formaldehyde with the Sensirion SFA30 over I2C on an ESP32"
date: 2026-07-31
track: iot-embedded
summary: "The SFA30 is an electrochemical formaldehyde sensor with a plain I2C interface: one address, four commands, three CRC-8-protected words per read, and a 10-second suppression window at power-up."
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

**Gist.** A metal-oxide (MOX) volatile organic compound (VOC) sensor collapses every reactive gas into a single index, so it cannot attribute a rise to formaldehyde (HCHO) specifically. The Sensirion SFA30 replaces the index with an electrochemical cell selective for HCHO, exposed over inter-integrated circuit (I2C) as four 16-bit commands returning three cyclic-redundancy-check-protected words. The cost is a protocol with several exact constants — address, scale factors, CRC parameters, settle time — every one of which silently produces plausible-looking wrong numbers when it is wrong.

## Why a dedicated formaldehyde channel

A MOX VOC sensor reports a total-VOC index or a computed equivalent carbon dioxide (eCO2) figure. Both are aggregate quantities: they respond to alcohols, terpenes, and cooking aerosols alike, and they drift with humidity. Formaldehyde is a distinct regulated indoor pollutant that off-gasses over months from particleboard, laminate flooring, adhesives and new textiles; the World Health Organization (WHO) indoor air quality guideline is **0.1 mg/m³ averaged over 30 minutes**, on the order of 80 parts per billion (ppb). An aggregate index cannot separate a furniture source from a cleaning-spray transient.

The SFA30 uses an electrochemical cell tuned for HCHO with low cross-sensitivity to ethanol, the usual false-positive gas for this class of measurement. The module carries an SHT humidity and temperature sensor alongside the cell, and offers two interfaces: I2C, and a universal asynchronous receiver-transmitter (UART) link speaking Sensirion's SHDLC protocol. This article covers the I2C path on an ESP32.

## The interface in four commands

The default 7-bit I2C address is **0x5D**. Every command is a 16-bit code transmitted most-significant byte first. Four commands cover normal operation.

| Command | Code | Notes |
|---|---|---|
| Start continuous measurement | `0x0006` | Sent once; the sensor then samples at approximately 2 Hz |
| Read measured values | `0x0327` | Returns 9 bytes: 3 words, each followed by its CRC |
| Get device marking | `0xD060` | 48-byte ASCII response (32 characters plus CRCs) |
| Device reset | `0xD304` | Soft reset, equivalent to a power cycle |

After a command is issued, **at least 5 ms must elapse before the response is read**. Values return as **signed** 16-bit big-endian integers, each 2-byte word followed by one CRC byte:

- **HCHO** = raw / 5 → ppb (range 0–1000 ppb, accuracy ±20 ppb or ±20 % of the measured value)
- **Relative humidity** = raw / 100 → %RH
- **Temperature** = raw / 200 → °C

The three divisors differ from one another, and the formaldehyde divisor — **5** — is the one least like the others. The read command is `0x0327`, distinct from the `0xD0xx` device-marking and reset codes.

## Warm-up and cadence

`0x0006` is sent once at start-up and moves the sensor into continuous measurement. The formaldehyde channel is **suppressed for the first 10 seconds of continuous measurement**, during which it reports 0 ppb. Humidity and temperature are valid immediately, which makes the two channels distinguishable during that window: a device returning valid environmental data and exactly zero HCHO is warming up, not miswired.

The sensor samples on its own schedule once started, at roughly 2 Hz. `0x0327` is a read of the most recent result rather than a trigger, so polling faster than the sampling rate repeats a value and polling slower returns the latest sample rather than an average of the ones missed. Staying in continuous measurement therefore avoids paying the 10-second suppression window on every reading.

## The CRC-8 and its exact parameters

Each 2-byte word carries an 8-bit CRC computed over those two data bytes. Sensirion's parameters are **polynomial 0x31, initial value 0xFF, no reflection on input or output, final XOR 0x00**. Because CRC-8 variants share the same shape and differ only in these constants, a wrong initial value or an unintended reflection does not produce an obvious error: it produces a checksum that never matches, so every otherwise-valid read is rejected as corrupt.

```c
uint8_t sfa30_crc(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;                     // init 0xFF, not 0x00
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;                             // final XOR is 0x00
}
```

The check is per word, not per frame. A single corrupted word therefore identifies which of the three channels is untrustworthy, and a driver may discard that channel while keeping the other two — though the simpler policy of rejecting the whole 9-byte frame is what the example below implements.

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
  Wire.write(cmd >> 8);                  // MSB first
  Wire.write(cmd & 0xFF);
  Wire.endTransmission();
}

void setup() {
  Serial.begin(115200);
  Wire.begin();                          // SDA=21, SCL=22 on most ESP32 dev boards
  sendCmd(0x0006);                       // start continuous measurement
  delay(10);
}

void loop() {
  sendCmd(0x0327);                       // read measured values
  delay(5);                              // minimum settle time before the read
  uint8_t buf[9];
  Wire.requestFrom(SFA30_ADDR, (uint8_t)9);
  for (int i = 0; i < 9 && Wire.available(); i++) buf[i] = Wire.read();

  bool ok = true;
  for (int w = 0; w < 3; w++)            // one CRC per word, three words
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

The three `int16_t` declarations are load-bearing. The words are documented as signed, and temperature is the channel that goes negative in practice; reassembling the bytes into an unsigned type maps every value below 0 °C onto the top of the 0–65535 range, which surfaces as a temperature near **327 °C** rather than as an obvious fault.

The 9-byte read is issued as a single `requestFrom` transaction. A stop condition ends the read, so splitting the frame into two shorter `requestFrom` calls does not resume where the first left off; what the second call returns is not the tail of the same frame.

## Pitfalls

- **Every read returns 0xFF, or the bus times out.** The address or the wiring is wrong. Confirm 0x5D with an `i2cdetect`-style bus scan before suspecting the protocol; SDA and SCL swapped produce the same symptom as a wrong address.
- **HCHO reads exactly 0 ppb while humidity and temperature look correct.** This is the documented 10-second suppression window at the start of continuous measurement, not a fault; it recurs whenever measurement is restarted, including after the soft reset `0xD304`.
- **Every frame fails CRC.** The CRC initial value is 0xFF; a routine initialised to 0x00, or one that reflects input or output bits, never matches for any input.
- **HCHO reads far too low, and never approaches the 1000 ppb ceiling.** The raw word was divided by a humidity- or temperature-style factor (100 or 200) rather than by 5.
- **Temperature reports a large positive value in a cold room.** The word was reassembled into an unsigned type, so the sign bit was read as magnitude.
- **A read issued immediately after the command returns stale or truncated data.** The 5 ms settle time is a minimum, not a suggestion.
- **HCHO is implausibly high after soldering or cleaning near the module.** Solvent and flux vapour is a real exposure to a cell this sensitive, so the reading is the sensor working, not failing.

**Extension.** Logging HCHO alongside a MOX VOC-index sensor such as the SGP40 on the same ESP32 makes the selectivity difference measurable: the two curves diverge when a source is formaldehyde-specific and track together when the event is a broadband VOC release.
