---
title: "SGP41 on an ESP32: raw ticks are meaningless — the Gas Index Algorithm isn't"
date: 2026-07-31
track: iot-embedded
summary: "Sensirion's SGP41 gives you two raw MOx signals that mean nothing in absolute terms — you have to run them through the Gas Index Algorithm to get the 1–500 VOC and NOx indices where 100 is 'recent normal'. Here's the I2C command flow, the CRC, the 1 Hz sampling rule, and an ESP32 sketch that wires conditioning → measure_raw → index."
reading_time: 6
tags: [sgp41, voc-index, nox-index, gas-index-algorithm, i2c, esp32, air-quality, sensirion]
sources:
  - title: "Datasheet SGP41 — VOC and NOx Sensor (Sensirion)"
    url: "https://sensirion.com/media/documents/5FE8673C/61E96F50/Sensirion_Gas_Sensors_Datasheet_SGP41.pdf"
  - title: "Sensirion/gas-index-algorithm — reference C library"
    url: "https://github.com/Sensirion/gas-index-algorithm"
  - title: "Sensirion/embedded-i2c-sgp41 — reference I2C driver"
    url: "https://github.com/Sensirion/embedded-i2c-sgp41"
  - title: "Sensirion/arduino-i2c-sgp41 + arduino-gas-index-algorithm"
    url: "https://github.com/Sensirion/arduino-gas-index-algorithm"
  - title: "SGP40/SGP41 VOC/NOx Sensor — ESPHome sgp4x component"
    url: "https://esphome.io/components/sensor/sgp4x/"
---

The SGP41 is Sensirion's two-pixel MOx (metal-oxide) gas sensor: one hot-plate for VOCs, a second run at a different temperature for NOx. Like every MOx part it doesn't measure a concentration — it measures the resistance of a heated tin-oxide film that shifts as gases adsorb on it. The chip hands you two raw numbers, `SRAW_VOC` and `SRAW_NOx`, each a 16-bit tick value (0–65535) that is *proportional to the logarithm of the film resistance*. That is the whole trap: those ticks are meaningless in absolute terms. 30000 doesn't mean "clean" and 40000 doesn't mean "bad" — the offset drifts with the individual sensor, the temperature, and how long it's been running.

## Why raw ticks need the Gas Index Algorithm

To get a number a human can act on, the raw ticks go through Sensirion's **Gas Index Algorithm**, a small statistics engine that runs on *your* MCU, not the sensor. It maintains an adaptive baseline — a recursively estimated mean and variance with a gain-offset normalization that decays exponentially over a rolling window (the default learning horizon is on the order of hours to a day). It then maps the current reading against that baseline to a **1–500 index**:

- **VOC Index**: 100 is the recent typical indoor composition (roughly the last 24 h of learned "normal"). Above 100 = deteriorating; below = cleaner than usual.
- **NOx Index**: the baseline sits at **1**, not 100 — an NOx event pushes it up toward 500. (This catches people out; the VOC and NOx offsets genuinely differ, and ESPHome even shipped a fix to default NOx `index_offset` to 1.)

Because it's adaptive and relative, the index self-calibrates and needs no reference gas — the cost is that it only reports *changes* against learned normal, not a lab-grade ppb figure.

## The I2C protocol: three commands, one CRC

The SGP41 lives at a fixed I2C address **`0x59`**. Commands are 16-bit, MSB first, and every data word on the wire (in either direction) carries a **CRC-8: polynomial `0x31` (x⁸+x⁵+x⁴+1), init `0xFF`**, no reflection, no final XOR — the same CRC every modern Sensirion part uses. The three commands you need:

| Command | Hex | Notes |
|---------|-----|-------|
| Execute Conditioning | `0x2612` | Runs the NOx pixel warm-up; call once at startup, **≤ 10 s total** |
| Measure Raw Signals | `0x2619` | Returns `SRAW_VOC` + `SRAW_NOx`; ~45 ms typ (50 ms max) |
| Turn Heater Off | `0x3615` | Back to idle/low power |

Both `0x2612` and `0x2619` take **two compensation words** — humidity then temperature — each followed by its CRC. Feeding real RH/T (e.g. from an SHT4x on the same bus) improves accuracy; if you don't have it, send the datasheet defaults: **RH `0x8000` = 50 %RH** and **T `0x6666` = 25 °C**. The conversions if you *do* have live values: `rh_ticks = %RH × 65535 / 100` and `t_ticks = (°C + 45) × 65535 / 175`.

Conditioning is not optional. On every power-up the NOx pixel needs ~10 s at its operating temperature before `measure_raw` gives trustworthy NOx ticks — but never run conditioning longer than 10 s or you degrade the sensing material.

## The 1 Hz rule

The single most important integration constraint: **sample at exactly 1 Hz**. The Gas Index Algorithm's baseline math assumes a constant 1-second interval; feed it at 0.5 Hz or in bursts and the index numbers are wrong, not just noisy. So the loop is: `delay(1000)` → read raw → `process()` → repeat, forever, at a steady cadence.

## ESP32: conditioning → measure_raw → index

Using Sensirion's Arduino libraries (`arduino-i2c-sgp41` for the sensor, `arduino-gas-index-algorithm` for the math — the latter wraps the same C `GasIndexAlgorithm_init` / `GasIndexAlgorithm_process` core):

```cpp
#include <Wire.h>
#include <SensirionI2CSgp41.h>
#include <VOCGasIndexAlgorithm.h>
#include <NOxGasIndexAlgorithm.h>

SensirionI2CSgp41  sgp41;              // I2C addr 0x59, fixed
VOCGasIndexAlgorithm voc_algorithm;     // 1 Hz baseline for VOC
NOxGasIndexAlgorithm nox_algorithm;     // 1 Hz baseline for NOx

uint16_t conditioning_s = 10;           // NOx warm-up, must not exceed 10 s

void setup() {
  Serial.begin(115200);
  Wire.begin();
  sgp41.begin(Wire);
}

void loop() {
  delay(1000);                          // <-- the 1 Hz rule, non-negotiable

  uint16_t rh = 0x8000;                 // 50 %RH default (swap in real SHT4x data)
  uint16_t t  = 0x6666;                 // 25 C default
  uint16_t srawVoc = 0, srawNox = 0;
  uint16_t err;

  if (conditioning_s > 0) {             // first ~10 s: condition the NOx pixel
    err = sgp41.executeConditioning(rh, t, srawVoc);   // cmd 0x2612
    conditioning_s--;
    return;                             // NOx not valid yet; keep the cadence
  }

  err = sgp41.measureRawSignals(rh, t, srawVoc, srawNox);  // cmd 0x2619, ~50 ms
  if (err) { Serial.println("measure error"); return; }

  int32_t vocIndex = voc_algorithm.process(srawVoc);  // 1..500, 100 = normal
  int32_t noxIndex = nox_algorithm.process(srawNox);  // 1..500, baseline 1

  Serial.printf("SRAW_VOC=%u SRAW_NOx=%u  VOC=%ld  NOx=%ld\n",
                srawVoc, srawNox, (long)vocIndex, (long)noxIndex);
}
```

Two things to expect on first run: the VOC index sits near its `index_offset` (100) until the baseline learns your room, and the NOx index will read close to 1 in clean air. Both take real minutes-to-hours of continuous 1 Hz data before they're genuinely calibrated — the state lives in RAM, so a reboot restarts learning unless you persist and restore the algorithm state.

## How this differs from BME690 / BSEC

Same MOx principle, opposite philosophy on where the smarts live. Bosch's BME690 pairs a raw gas-resistance reading (in ohms) with **BSEC**, a closed-source fusion library that also does gas *classification* via a model you train in AI Studio. Sensirion splits it cleanly: an open-source, self-contained Gas Index Algorithm (BSD-licensed C, no training, no blob) that only ever produces the two indices. If you want a transparent, retrain-free VOC/NOx number, SGP41 + Gas Index Algorithm is the simpler stack; if you want to distinguish coffee from solvent, that's BME690/BSEC territory (covered in a sibling article).

**Try next:** add an SHT4x on the same I2C bus, feed its live RH/T (converted with the tick formulas above) into `measureRawSignals` instead of the `0x8000`/`0x6666` defaults, and compare VOC-index stability while you breathe humid air on the sensor — then persist the algorithm state to NVS so a reboot doesn't throw away hours of baseline learning.
