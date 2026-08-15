---
title: "SGP41 on an ESP32: raw ticks are meaningless, the Gas Index Algorithm is not"
date: 2026-07-31
track: iot-embedded
summary: "Sensirion's SGP41 emits two raw metal-oxide signals with no absolute meaning; the Gas Index Algorithm converts them into 1–500 VOC and NOx indices anchored on a learned baseline. This article covers the I2C command flow, the CRC-8 parameters, the 1 Hz sampling constraint, and an ESP32 sketch wiring conditioning to measure_raw to index."
reading_time: 7
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

**Gist.** A metal-oxide (MOx) gas sensor reports the resistance of a heated tin-oxide film, not a concentration, so the SGP41's two 16-bit raw signals carry no absolute meaning: the offset varies with the individual part, the ambient conditions, and the elapsed runtime. Sensirion's Gas Index Algorithm resolves this by running an adaptive baseline estimator on the host microcontroller and reporting the current reading as a **1–500 index relative to learned normal**, which removes the need for reference-gas calibration. The cost is that the output is a *relative* signal with a state that must be learned over time, and its arithmetic assumes a strictly regular sampling interval.

## What the raw signals are

The SGP41 carries two MOx pixels: one hot-plate optimised for volatile organic compounds (VOCs) and a second operated at a different temperature for nitrogen oxides (NOx). The device returns `SRAW_VOC` and `SRAW_NOx`, each a 16-bit tick value in the range 0–65535 that is **proportional to the logarithm of the film resistance**. Neither value maps to a fixed air-quality level. A reading of 30000 does not denote clean air and 40000 does not denote poor air; only the change of a given sensor's value against its own recent history is interpretable.

## The Gas Index Algorithm

The conversion to an actionable number happens in the **Gas Index Algorithm**, a statistics engine that executes on the host microcontroller unit (MCU) rather than inside the sensor. It maintains an adaptive baseline: a recursively estimated mean and variance of the raw signal that decays over a rolling window whose default learning horizon is measured in hours. The current sample is scored against that baseline and mapped to a **1–500 index**.

The two indices are anchored differently, and this asymmetry is the most common source of misread data:

- **VOC Index.** The offset is **100**, representing the recently learned typical indoor composition. Values above 100 indicate deterioration relative to that baseline; values below indicate air cleaner than the learned normal.
- **NOx Index.** The offset is **1**, not 100. Clean air reads near 1 and an NOx event drives the index upward toward 500. ESPHome's `sgp4x` component exposes the offset as a per-sensor `index_offset` option, defaulted separately for the two indices.

Because the estimator is adaptive and relative, it self-calibrates without a reference gas. The corresponding limitation is that it reports **changes against learned normal, not a laboratory-grade parts-per-billion figure**. A room whose baseline pollution is constant and high converges to an index near the offset, because the algorithm has no external anchor by which to call that baseline abnormal.

## The I2C protocol: three commands, one CRC

The SGP41 occupies the fixed Inter-Integrated Circuit (I2C) address **`0x59`**. Commands are 16-bit, most significant byte first. Every data word on the bus, in either direction, is followed by a **cyclic redundancy check (CRC-8) with polynomial `0x31` (x⁸ + x⁵ + x⁴ + 1), initialisation `0xFF`, no input or output reflection, and no final XOR** — the same parameters used across modern Sensirion parts. A word whose CRC fails must be discarded rather than clamped or interpolated, since a corrupted tick value propagates into the baseline estimator and persists for the length of the learning horizon.

| Command | Hex | Notes |
|---------|-----|-------|
| Execute Conditioning | `0x2612` | Runs the NOx pixel warm-up; issued once at startup, **≤ 10 s total** |
| Measure Raw Signals | `0x2619` | Returns `SRAW_VOC` and `SRAW_NOx`; measurement duration **50 ms maximum** |
| Turn Heater Off | `0x3615` | Returns the device to idle / low power |

Both `0x2612` and `0x2619` accept **two compensation words** — relative humidity first, then temperature — each followed by its own CRC. Supplying live humidity and temperature, for example from an SHT4x sharing the bus, improves accuracy. Where no such measurement exists, the datasheet defaults are **relative humidity `0x8000` = 50 %RH** and **temperature `0x6666` = 25 °C**. With live values the tick conversions are `rh_ticks = %RH × 65535 / 100` and `t_ticks = (°C + 45) × 65535 / 175`.

Conditioning is mandatory rather than advisory. After each power-up the NOx pixel requires roughly 10 s at its operating temperature before `measure_raw` yields trustworthy NOx ticks. The bound is two-sided: the datasheet also states that **conditioning must not be run for longer than 10 s**, so the warm-up is a fixed-length startup phase rather than something to extend while other initialisation completes.

## The 1 Hz constraint

The dominant integration constraint is the sampling cadence: **the Gas Index Algorithm assumes a fixed, regular interval between calls to `process()`, and defaults to one second**. The interval is declared once, at initialisation — the C library exposes `GasIndexAlgorithm_init_with_sampling_interval()` and the Arduino wrapper takes the same value as a constructor argument — and the decay constants are derived from it. It is never passed to `process()` itself, so the algorithm cannot observe that a call arrived late. Feeding it in bursts, or at a rate other than the one declared, therefore does not merely add noise: it rescales the effective learning horizon. The control loop is a fixed cycle: wait one second, read raw signals, call `process()`, repeat indefinitely.

## ESP32 integration: conditioning, measure_raw, index

The sketch below uses Sensirion's Arduino libraries: `arduino-i2c-sgp41` for the transport and `arduino-gas-index-algorithm` for the estimator, the latter wrapping the same `GasIndexAlgorithm_init` and `GasIndexAlgorithm_process` core as the C reference library.

```cpp
#include <Wire.h>
#include <SensirionI2CSgp41.h>
#include <VOCGasIndexAlgorithm.h>
#include <NOxGasIndexAlgorithm.h>

SensirionI2CSgp41   sgp41;              // fixed I2C address 0x59
VOCGasIndexAlgorithm voc_algorithm;     // default 1 s sampling interval, offset 100
NOxGasIndexAlgorithm nox_algorithm;     // default 1 s sampling interval, offset 1

uint16_t conditioning_s = 10;           // NOx warm-up; must not exceed 10 s

void setup() {
  Serial.begin(115200);
  Wire.begin();
  sgp41.begin(Wire);
}

void loop() {
  delay(1000);                          // must match the interval process() was built with

  uint16_t rh = 0x8000;                 // 50 %RH default; replace with SHT4x ticks
  uint16_t t  = 0x6666;                 // 25 C default
  uint16_t srawVoc = 0, srawNox = 0;
  uint16_t err;

  if (conditioning_s > 0) {             // first ~10 s: condition the NOx pixel
    err = sgp41.executeConditioning(rh, t, srawVoc);      // command 0x2612
    if (err) Serial.println("conditioning error");
    conditioning_s--;
    return;                             // NOx invalid; the cadence is preserved
  }

  err = sgp41.measureRawSignals(rh, t, srawVoc, srawNox); // 0x2619, <= 50 ms
  if (err) { Serial.println("measure error"); return; }   // skip, do not substitute

  int32_t vocIndex = voc_algorithm.process(srawVoc);      // 1..500, offset 100
  int32_t noxIndex = nox_algorithm.process(srawNox);      // 1..500, offset 1

  Serial.printf("SRAW_VOC=%u SRAW_NOx=%u  VOC=%ld  NOx=%ld\n",
                srawVoc, srawNox, (long)vocIndex, (long)noxIndex);
}
```

Two behaviours are expected on a first run. The VOC index sits near its offset of 100 until the baseline has learned the room, and the NOx index reads close to 1 in clean air. Both require continuous 1 Hz data over minutes to hours before the baseline is representative. **The estimator state lives in RAM, so any reboot restarts learning** unless the state is persisted and restored explicitly.

## Comparison with BME690 and BSEC

Both devices rest on the same MOx principle but place the processing differently. Bosch's BME690 pairs a raw gas-resistance reading, expressed in ohms, with **BSEC**, a closed-source fusion library that additionally performs gas *classification* using a model trained in AI Studio. Sensirion separates the layers: an open-source, self-contained Gas Index Algorithm distributed as BSD-licensed C, requiring no training data and no binary blob, which produces exactly the two indices and nothing else. Distinguishing one gas species from another is outside the SGP41 stack's output and falls to the BME690 and BSEC combination, covered in a sibling article.

## Pitfalls

- **Interpreting a NOx index of 1 as a sensor fault.** The NOx offset is 1, so clean air legitimately reads at the bottom of the scale; code that treats "1" as an error sentinel discards valid data.
- **Comparing raw ticks across devices or sessions.** `SRAW_VOC` is proportional to log resistance with a per-part, temperature- and runtime-dependent offset, so a threshold tuned on one unit does not transfer.
- **Sampling on a timer that drifts or bursts.** The interval is fixed at initialisation and never passed to `process()`, so a late or bunched call is indistinguishable from an on-time one; an irregular cadence rescales the decay rather than adding noise.
- **Running conditioning beyond 10 s, for example by looping it while waiting for a network connection.** The datasheet bounds conditioning at 10 s; holding the NOx pixel there indefinitely runs the part outside its specified operating sequence.
- **Skipping conditioning entirely after a reboot.** The NOx pixel has not reached operating temperature, so early NOx ticks feed unrepresentative values into a baseline that then takes the full learning horizon to recover.
- **Discarding algorithm state on reset without persisting it.** The estimator restarts from its initial baseline, so indices immediately after a power cycle reflect no learned history despite appearing well-formed.
- **Ignoring a CRC-8 failure and using the word anyway.** A corrupted tick enters the recursive mean and variance estimate and biases the index for as long as that sample remains inside the rolling window.
- **Leaving the compensation words at the defaults while logging humid-air experiments.** The algorithm receives 50 %RH and 25 °C regardless of actual conditions, so humidity-driven resistance shifts are attributed to gas events.
