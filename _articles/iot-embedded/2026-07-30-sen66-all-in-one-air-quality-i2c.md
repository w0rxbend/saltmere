---
title: "The Sensirion SEN66: one I2C module for PM, RH/T, VOC, NOx and CO2"
date: 2026-07-30
track: iot-embedded
summary: "The SEN66 adds a true onboard CO2 sensor to the environmental node that the SEN5x lacked. The addition brings a new command set, a new I2C address (0x6B) and a nine-value read. This article places it in the SEN6x family, enumerates the differences from the SEN5x, and gives a working ESP32 sketch on the official library."
reading_time: 6
tags: [esp32, sen66, sen6x, i2c, co2, air-quality, sensirion]
sources:
  - title: "SEN6x Datasheet (Sensirion)"
    url: "https://sensirion.com/resource/datasheet/SEN6x"
  - title: "SEN66 product page — PM, RH/T, VOC, NOx and CO2 (Sensirion)"
    url: "https://sensirion.com/products/catalog/SEN66"
  - title: "Sensirion/arduino-i2c-sen66 — official Arduino I2C driver"
    url: "https://github.com/Sensirion/arduino-i2c-sen66"
  - title: "Sensirion/embedded-i2c-sen66 — generic embedded C driver"
    url: "https://github.com/Sensirion/embedded-i2c-sen66"
  - title: "EYE on NPI: Sensirion SEN66 Environmental Sensor Node (Adafruit)"
    url: "https://blog.adafruit.com/2025/05/08/eye-on-npi-sensirion-sen66-environmental-sensor-node-eyeonnpi-digikey-digikey-sensirion-adafruit/"
---

**Gist.** A SEN5x node measures particulate matter (PM), relative humidity (RH), temperature, volatile organic compounds (VOC) and nitrogen oxides (NOx), but carries no carbon dioxide (CO2) sensor, so designs that need CO2 add a second device such as an SCD41 to the same bus. The **SEN66** integrates a CO2 sensing element into the same fan-cooled housing behind one inter-integrated circuit (I2C) interface, yielding nine measured values from a single read. The cost is that firmware written for the SEN5x does not carry over unchanged: the I2C address, the command opcodes and the response length all differ, and the CO2 channel has its own conditioning and calibration behaviour.

## Position within the SEN6x family

The SEN66 belongs to the **SEN6x** generation, which supersedes the SEN5x. Members of the family differ by which gas sensing elements are fitted inside the same housing:

| Part | PM | RH/T | VOC/NOx | CO2 | HCHO |
|------|:--:|:----:|:-------:|:---:|:----:|
| **SEN60** | ✓ | | | | |
| **SEN63C** | ✓ | ✓ | | ✓ | |
| **SEN65** | ✓ | ✓ | ✓ | | |
| **SEN66** | ✓ | ✓ | ✓ | ✓ | |
| **SEN68** | ✓ | ✓ | ✓ | | ✓ (formaldehyde) |

The SEN66 therefore covers every channel except formaldehyde (HCHO): **PM1.0, PM2.5, PM4, PM10, humidity, temperature, VOC index, NOx index and CO2**. The CO2 channel is the sole functional difference from the SEN65. It is a gas measurement produced by Sensirion's compact photoacoustic CO2 technology, the same class of element as the standalone SCD4x, **not a VOC-derived "eCO2" estimate**. The two quantities are not substitutes: a VOC index responds to solvents and cooking emissions, while CO2 concentration tracks occupancy and ventilation rate.

## Differences from the SEN5x

Four changes break a direct firmware port:

- **The I2C address moved.** The SEN5x answers at `0x69`. The SEN66, and its siblings SEN63C, SEN65 and SEN68, answer at **`0x6B`**. The PM-only SEN60 uses `0x6C`.
- **The command set changed with the generation.** On the SEN66, **Start Continuous Measurement is `0x0021`** and **Read Measured Values is `0x0300`**, where the SEN5x used `0x03C4` for the latter. The SEN5x opcode table is not transferable.
- **The read returns nine values, not eight.** Four PM sizes, RH, temperature, VOC index, NOx index, and a CO2 word in parts per million (ppm).
- **Only one physical interface.** The SEN66 exposes I2C alone; the SEN5x additionally offered a universal asynchronous receiver/transmitter (UART) mode selected by a pin.

The on-the-wire framing is unchanged and is the part worth reusing. Commands are 16-bit and sent most-significant byte first, and **every returned 2-byte word is followed by a cyclic redundancy check (CRC-8)** using the same parameters as the SEN5x, SCD4x and SHT4x: **polynomial `0x31`, initialisation `0xFF`**, no input or output reflection, no final XOR. A `sensirion_crc()` routine written for the [SEN5x raw-I2C article](/articles/iot-embedded/2026-07-30-sen5x-raw-i2c-crc/) applies to a SEN66 driver unchanged; only addresses and opcodes are new.

## Measurement flow

The lifecycle follows the established Sensirion sequence with the new opcodes:

1. **Reset** — issue `Device Reset` and wait approximately **1.2 s** for the module to boot. Optional, but it puts the device into a known state.
2. **Start** — issue `Start Continuous Measurement` (`0x0021`). Sampling proceeds on a **1 s cadence**, and the **first valid results appear roughly 1.1 s later**.
3. **Read** — issue `Read Measured Values` (`0x0300`), wait the execution time of approximately **20 ms**, then read back the nine words. Each word is 2 data bytes plus 1 CRC byte, so the transfer is **27 bytes**. Polling faster than once per second returns no new information, because fresh data lands only every ~1 s.

The official library performs the CRC verification and byte unpacking and returns scaled values, which is why the sketch below uses it rather than re-deriving raw reads; the raw path is identical in structure to the SEN5x case.

## Wiring on an ESP32

The SEN66 contains a fan for the particulate stage and therefore requires **5 V on VDD**. Its I2C pins are compatible with 3.3 V logic and connect directly to an ESP32 without a level shifter.

| SEN66 pin | ESP32 |
|-----------|-------|
| VDD | 5V |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |
| SEL | GND (selects I2C) |

**SEL tied to GND** locks the module into I2C mode. **Pull-up resistors** on SDA and SCL are required where the breakout board does not already provide them; the SEN6x datasheet gives the recommended value.

## Firmware using the official library

The **Sensirion I2C SEN66** library is installed from the Arduino Library Manager and depends on **Sensirion Core**. Its interface mirrors the SEN5x driver, so a port amounts to a class-name and address change plus the additional `co2` output.

```cpp
#include <Arduino.h>
#include <SensirionI2cSen66.h>
#include <Wire.h>

SensirionI2cSen66 sensor;

void setup() {
  Serial.begin(115200);
  Wire.begin();                          // default SDA=21, SCL=22

  sensor.begin(Wire, SEN66_I2C_ADDR_6B); // 0x6B — not the SEN5x's 0x69
  sensor.deviceReset();
  delay(1200);                           // module reboot time

  sensor.startContinuousMeasurement();   // 1 s sampling cadence
  delay(1100);                           // first results ready ~1.1 s later
}

void loop() {
  float pm1, pm25, pm4, pm10, rh, tempC, voc, nox;
  uint16_t co2;                          // CO2 is returned as ppm, not a float

  int16_t err = sensor.readMeasuredValues(
      pm1, pm25, pm4, pm10, rh, tempC, voc, nox, co2);

  if (err != 0) {                        // 0 == NO_ERROR
    Serial.println("SEN66 read error");
  } else {
    Serial.printf(
      "PM2.5=%.1f ug/m3  T=%.1fC  RH=%.0f%%  VOC=%.0f  NOx=%.0f  CO2=%u ppm\n",
      pm25, tempC, rh, voc, nox, co2);
  }
  delay(1000);                           // stay within the 1 s cadence
}
```

The signature mixes types: **`co2` is a `uint16_t` in ppm** while the PM, RH, temperature, VOC and NOx channels arrive as scaled floats. The driver has already applied the datasheet scale factors — **PM ÷ 10, RH ÷ 100, temperature ÷ 200, VOC and NOx ÷ 10** — and validated the CRC of each word.

Adding a `getSerialNumber()` call after `begin()` confirms that the device answering at `0x6B` is a SEN66 before any reading is trusted, and supplies a stable hardware identifier for each node in a fleet.

## Warm-up and conditioning

Two timescales apply, and conflating them produces readings that appear wrong.

The **module-level timescale** is the ~1.1 s to first data covered by the delays above. The **gas-channel timescale** is far longer. The **VOC and NOx outputs are indices produced by an adaptive algorithm** that references the current reading against a learned baseline: the VOC index reports 100 for typical conditions and the NOx index reports 1, and both continue to adapt to the environment for hours after power-on, so early values are not erroneous but are not yet referenced to the room. The **CO2 element behaves as an SCD4x-class part**, so the readings immediately after a start command should be treated as settling rather than as calibrated concentrations.

For long-running nodes, the CO2 channel's **automatic self-calibration (ASC)** assumes the space periodically returns to a fresh-air baseline of approximately 400 ppm. In an enclosure or room that never reaches that baseline, the assumption does not hold and the reading drifts; ASC should then be disabled and calibration performed manually.

Continuous mode drives both the fan and the CO2 element continuously, so it is the most power-hungry way to run the module. A battery-powered node has to consult the measurement modes and the current-consumption table in the SEN6x datasheet rather than assume continuous operation is the only option.

## Pitfalls

- **Reusing the SEN5x address `0x69`.** The bus scan finds nothing at `0x69` and every transaction fails, because the SEN66 answers only at `0x6B` (and the SEN60 at `0x6C`).
- **Reusing the SEN5x opcode `0x03C4` for Read Measured Values.** The command is not part of the SEN6x set; the correct opcode is `0x0300`.
- **Sizing the read buffer for eight values.** The SEN66 response is nine words, that is 27 bytes including one CRC byte per word; a 24-byte buffer truncates the CO2 word and leaves the trailing bytes of the response unread.
- **Polling faster than once per second.** Repeated reads return the same sample, since new data is produced only on the ~1 s cadence.
- **Reading immediately after `startContinuousMeasurement()`.** The first valid results appear approximately 1.1 s after the start command; earlier reads do not reflect a completed measurement.
- **Treating early VOC and NOx values as absolute concentrations.** They are indices relative to a learned baseline — 100 for typical VOC conditions, 1 for typical NOx — and they shift as the algorithm adapts over the hours after power-on.
- **Leaving ASC enabled in a space that never reaches a fresh-air baseline.** The self-calibration assumes periodic exposure to approximately 400 ppm; without it the CO2 reading drifts.
- **Omitting bus pull-ups.** Where the breakout does not carry them, SDA and SCL need external pull-ups or the bus never returns to the idle high level.
- **Leaving SEL floating.** The pin must be tied to GND to select I2C mode.
- **Powering VDD from 3.3 V.** The internal fan for the particulate stage requires 5 V on VDD; the I2C lines remain 3.3 V-logic compatible.
