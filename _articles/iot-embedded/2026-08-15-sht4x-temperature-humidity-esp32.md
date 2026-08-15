---
title: "SHT4x: The Boring, Excellent Temp/RH Sensor Your Air-Quality Node Needs"
date: 2026-08-15
track: iot-embedded
summary: "Sensirion's SHT4x family is the least exciting part on an air-quality node and the one I trust most: ±1.0 %RH on the SHT45, a single-command I2C protocol you can drive without a library, and an on-chip heater that undoes condensation and creep. Here's the protocol down to the CRC-8, why a discrete RH/T sensor beats the one buried inside your PM sensor's warm box, and how to mount it so the numbers mean something."
reading_time: 5
tags: [esp32, sht4x, sensirion, i2c, humidity, air-quality, esp-idf]
sources:
  - title: "Sensirion SHT4x Datasheet (v7.3, June 2026)"
    url: "https://sensirion.com/media/documents/33FD6951/6A7C10A0/HT_DS_Datasheet_SHT4x_V7.3.pdf"
  - title: "SHT45 — Sensirion product page"
    url: "https://sensirion.com/products/catalog/SHT45"
  - title: "Adafruit SHT40 Temperature & Humidity Sensor guide"
    url: "https://learn.adafruit.com/adafruit-sht40-temperature-humidity-sensor"
  - title: "I2C Master Driver — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/i2c.html"
---

Every air-quality node I've built has a particulate sensor as the headline act and a temperature/humidity sensor as an afterthought — and the afterthought is the reading people actually check daily. The sensor I keep coming back to is Sensirion's **SHT4x** family: fourth-generation, factory-calibrated, absurdly low power, and speaking an I2C protocol so simple you can implement it from the datasheet in twenty minutes without a vendor library.

## Pick your accuracy class

The family is one die, three bins. All share the same protocol, package, and 1.08–3.6 V supply; you pay for tighter calibration:

| Part | Typ. RH accuracy | Typ. T accuracy | When |
|---|---|---|---|
| **SHT40** | ±1.8 %RH (max ±3.5) | ±0.2 °C | General telemetry |
| **SHT41** | ±1.8 %RH (max ±2.5) | ±0.2 °C | Same typ., tighter guarantee |
| **SHT45** | ±1.0 %RH | ±0.1 °C | Reference node, dew-point math |

Average supply current is around **0.4 µA** at one low-precision measurement per second — the sensor is idle (and drawing nanoamps) except during the few milliseconds of conversion, which suits a deep-sleeping node perfectly. For dew-point or absolute-humidity calculations the errors compound, so the SHT45's ±1.0 %RH is worth the extra ~two dollars on the one node you treat as ground truth.

## The protocol: one command, six bytes, one CRC

The standard parts sit at I2C address **0x44** (B/C variants at 0x45/0x46 let you hang two on one bus). There are no registers. You write a single command byte, wait, and read six bytes back: temperature MSB/LSB, CRC, humidity MSB/LSB, CRC.

- `0xFD` — measure, **high precision** (≤ 8.3 ms)
- `0xF6` — medium (≤ 4.5 ms), `0xE0` — low (≤ 1.6 ms)
- `0x89` — read serial number, `0x94` — soft reset

Each CRC is **CRC-8, polynomial 0x31, init 0xFF** over the preceding two bytes. Check it: I2C on a node with a PM sensor's fan motor nearby *will* occasionally corrupt a transfer. Conversion is fixed-point friendly: T = −45 + 175·S<sub>T</sub>/65535 °C, RH = −6 + 125·S<sub>RH</sub>/65535 % (clip to 0–100).

```c
static uint8_t crc8(const uint8_t *d, int n)      /* poly 0x31, init 0xFF */
{
    uint8_t crc = 0xFF;
    for (int i = 0; i < n; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++)
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
    return crc;
}

esp_err_t sht4x_read(i2c_master_dev_handle_t dev, float *t_c, float *rh)
{
    const uint8_t cmd = 0xFD;                     /* single-shot, high precision */
    uint8_t rx[6];

    ESP_RETURN_ON_ERROR(i2c_master_transmit(dev, &cmd, 1, 100), "sht4x", "tx");
    vTaskDelay(pdMS_TO_TICKS(10));                /* t_meas <= 8.3 ms */
    ESP_RETURN_ON_ERROR(i2c_master_receive(dev, rx, 6, 100), "sht4x", "rx");

    if (crc8(rx, 2) != rx[2] || crc8(rx + 3, 2) != rx[5])
        return ESP_ERR_INVALID_CRC;

    *t_c = -45.0f + 175.0f * ((rx[0] << 8) | rx[1]) / 65535.0f;
    float h = -6.0f + 125.0f * ((rx[3] << 8) | rx[4]) / 65535.0f;
    *rh = h < 0 ? 0 : (h > 100 ? 100.0f : h);
    return ESP_OK;
}
```

That's the whole driver. No init sequence, no configuration registers, no mode state machine to get wrong after a brownout.

## The heater: undoing condensation and creep

Polymer RH sensors drift upward when parked in high humidity for weeks (**creep**) and read pinned at ~100 % after **condensation** until the element dries. The SHT4x's answer is an on-chip heater with six commands — 20/110/200 mW for 0.1 s or 1 s each (e.g. `0x39` = 200 mW for 1 s) — that bakes the element dry and takes a measurement at the end of the pulse. For a greenhouse or bathroom node, a 200 mW pulse once every few hours keeps readings honest. Respect the datasheet limits: at most **10 % heater duty cycle** over the sensor's life, and don't fire it above 65 °C ambient. Heater-on readings are for recovery, not reporting — the die is hot, so discard the RH/T that comes back with the pulse or use it only as a "did it dry out" check.

## Why not just use the RH/T inside your PM sensor?

An SEN5x or similar all-in-one reports RH/T too, so why add a part? Because that sensor lives *inside a box with a fan, a laser, and your ESP32* — typically 1–3 °C above ambient from self-heating. The vendor firmware applies compensation, but it's tuned for their reference enclosure, not yours. Temperature error propagates straight into RH (warm air reads drier: +2 °C ≈ −5 to −8 %RH at room conditions) and into any dew-point output. A discrete SHT4x costs ~$2–4 and gives you a measurement point *you* control: outside the warm zone, in ambient airflow. It's also your cross-check — when the integrated sensor and the SHT4x diverge over months, you've caught drift you'd otherwise never see.

Mounting rules that matter more than the accuracy class: put the sensor on a stub or daughterboard away from the ESP32 and regulator, or isolate it with **milled slots in the PCB** around its footprint (copper is an excellent heater-to-sensor conduit); give the enclosure vent slots so air actually exchanges; keep it out of direct sunlight and away from the PM fan's exhaust; and don't conformal-coat over the membrane opening.

**Try next:** log your PM-box's internal RH/T against a discrete SHT4x mounted outside the warm zone for a week — the offset curve you get is your enclosure's self-heating signature, and worth compensating explicitly.
