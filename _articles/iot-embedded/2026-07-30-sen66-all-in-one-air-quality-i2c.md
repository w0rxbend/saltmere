---
title: "The Sensirion SEN66: one I2C module for PM, RH/T, VOC, NOx — and real CO2"
date: 2026-07-30
track: iot-embedded
summary: "The SEN66 is Sensirion's newest all-in-one air node, and its headline feature is a true onboard CO2 sensor that the SEN5x never had. That addition brings a new command set, a new I2C address (0x6B), and a nine-value read. Here's where it sits in the SEN6x family, how it differs from the SEN5x, and a working ESP32 sketch on the official library."
reading_time: 5
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

If you've built a SEN5x node, you know the frustration: it does particulates, humidity, temperature, VOC and NOx beautifully — but there's no real CO2. You end up bolting an SCD41 onto the same bus as a second device. The **SEN66** collapses that into one part. It's Sensirion's newest environmental module, and it adds a **true onboard CO2 sensor** to everything the SEN55 already did, behind a single I2C interface. One module, nine numbers.

## Where the SEN66 sits: the SEN6x family

The SEN66 is one member of the **SEN6x** generation, which supersedes the SEN5x. The family scales by which gas sensors are stuffed inside the same fan-cooled housing:

| Part | PM | RH/T | VOC/NOx | CO2 | HCHO |
|------|:--:|:----:|:-------:|:---:|:----:|
| **SEN60** | ✓ | | | | |
| **SEN63C** | ✓ | ✓ | | ✓ | |
| **SEN65** | ✓ | ✓ | ✓ | | |
| **SEN66** | ✓ | ✓ | ✓ | ✓ | |
| **SEN68** | ✓ | ✓ | ✓ | | ✓ (formaldehyde) |

So the SEN66 is the "everything except formaldehyde" part: **PM1.0/PM2.5/PM4/PM10, humidity, temperature, VOC index, NOx index, and CO2** in one module. The CO2 channel is the whole reason to reach for it over a SEN65 — it's a genuine gas measurement (Sensirion's compact photoacoustic CO2 technology, the same class as the standalone SCD4x), not a VOC-derived "eCO2" estimate. That distinction matters: a VOC index tracks solvents and cooking; real CO2 tracks how many people are breathing in the room and whether your ventilation keeps up.

## What actually changed from the SEN5x

If you're porting a SEN5x design, four things are different — and they'll bite if you assume the old firmware "just works":

- **The I2C address moved.** SEN5x lived at `0x69`. The SEN66 (and its CO2/VOC siblings SEN63C/SEN65/SEN68) answer at **`0x6B`**. The bare PM-only SEN60 is different again at `0x6C`.
- **New command set.** The opcodes changed with the generation. On the SEN66, **Start Continuous Measurement is `0x0021`** and **Read Measured Values is `0x0300`** — not the SEN5x's `0x03C4`. Don't copy the old command table.
- **Nine values, not eight.** The read returns the four PM sizes, RH, temperature, VOC index, NOx index, *and* a CO2 word in ppm.
- **Single interface.** The SEN66 exposes only I2C (the older SEN5x also had a legacy UART/SEN5x-select pin). One bus, one address, done.

What did *not* change is the thing worth reusing: the **on-the-wire framing**. Commands are 16-bit, sent MSB-first, and every returned 2-byte word is followed by a **CRC-8** — the same Sensirion CRC used across the SEN5x, SCD4x and SHT4x: **polynomial `0x31`, init `0xFF`**, no reflection, no final XOR. If you already wrote a `sensirion_crc()` for the [SEN5x raw-I2C article](/articles/iot-embedded/2026-07-30-sen5x-raw-i2c-crc/), it drops straight into a SEN66 driver unchanged. Only the addresses and opcodes are new.

## The measurement flow

The lifecycle is the familiar Sensirion pattern, just with the new opcodes:

1. **Reset** (optional but clean): `Device Reset`, then wait ~1.2 s for the module to boot.
2. **Start**: send `Start Continuous Measurement` (`0x0021`). The sensor begins sampling on a **1 s cadence**; the **first valid results appear ~1.1 s later**.
3. **Read**: send `Read Measured Values` (`0x0300`), wait the ~20 ms execution time, then read back the nine words (each 2 data bytes + 1 CRC byte = 27 bytes total). Poll this no faster than once per second — new data only lands every ~1 s.

The official library hides the CRC and the byte packing and hands you floats, which is the right call for a real node. Below I use it directly rather than re-deriving the raw reads (the raw path is covered in the SEN5x piece and is byte-for-byte the same idea here).

## Wiring on an ESP32

Four wires plus a fan supply. The SEN66 has an internal fan for the particulate stage, so it wants **5 V on VDD**; its I2C pins are 3.3 V-logic friendly, so they connect straight to the ESP32 with no level shifter:

| SEN66 pin | ESP32 |
|-----------|-------|
| VDD | 5V |
| GND | GND |
| SDA | GPIO21 |
| SCL | GPIO22 |
| SEL | GND (selects I2C) |

Tie **SEL to GND** to lock the module into I2C mode, and put **4.7 kΩ pull-ups** on SDA/SCL if your breakout board doesn't already carry them.

## The firmware, official library

Install **Sensirion I2C SEN66** from the Library Manager (it pulls in **Sensirion Core** as a dependency). The API mirrors the SEN5x driver, so the port is mostly a class-name and address swap — plus the new `co2` output:

```cpp
#include <Arduino.h>
#include <SensirionI2cSen66.h>
#include <Wire.h>

SensirionI2cSen66 sensor;

void setup() {
  Serial.begin(115200);
  Wire.begin();                          // default SDA=21, SCL=22

  sensor.begin(Wire, SEN66_I2C_ADDR_6B); // 0x6B — NOT the SEN5x's 0x69
  sensor.deviceReset();
  delay(1200);                           // let the module reboot

  sensor.startContinuousMeasurement();   // 1 s sampling cadence
  delay(1100);                           // first results ready ~1.1 s later
}

void loop() {
  float pm1, pm25, pm4, pm10, rh, tempC, voc, nox;
  uint16_t co2;                          // CO2 comes back as ppm, not a float

  int16_t err = sensor.readMeasuredValues(
      pm1, pm25, pm4, pm10, rh, tempC, voc, nox, co2);

  if (err != 0) {                        // 0 == NO_ERROR
    Serial.println("SEN66 read error");
  } else {
    Serial.printf(
      "PM2.5=%.1f ug/m3  T=%.1fC  RH=%.0f%%  VOC=%.0f  NOx=%.0f  CO2=%u ppm\n",
      pm25, tempC, rh, voc, nox, co2);
  }
  delay(1000);                           // don't out-run the 1 s cadence
}
```

Note the library returns **`co2` as a `uint16_t` in ppm**, while the PM/RH/T/VOC/NOx channels arrive as scaled floats — the driver has already applied the datasheet scale factors (PM ÷10, RH ÷100, T ÷200, VOC/NOx ÷10) and unpacked the CRC-checked words for you.

## Warm-up and conditioning — CO2 is the slow one

Two timescales matter. The **module boots** in ~1.1 s to first data, which is what the delays above cover. But the **gas channels condition** over much longer. The **VOC and NOx indices are adaptive**: they start near a baseline of 100 and learn your environment over hours to days, so a fresh reading isn't wrong, it's just uncalibrated to the room yet. The **CO2 sensor** behaves like any SCD4x-class part — treat the first minute of readings as settling, and for long-running nodes lean on its automatic self-calibration (ASC), which assumes the space periodically returns to ~400 ppm fresh-air baseline. If your node lives somewhere that never sees fresh air, disable ASC and calibrate manually, or you'll slowly drift.

For a battery node, the fan and CO2 sensor make continuous mode power-hungry; the SEN66 also has periodic/single-shot style measurement modes worth reading up on in the datasheet before you commit to always-on sampling.

**Try next:** Add `getSerialNumber()` after `begin()` and log it — it confirms you're actually talking to a SEN66 at `0x6B` before you trust a single reading, and gives each node in a fleet a stable hardware ID. Then chart CO2 against VOC for a day: watching CO2 climb when the room fills and VOC spike when you cook is the clearest demonstration of why the SEN66's extra gas channel earns its place over a plain SEN65.
