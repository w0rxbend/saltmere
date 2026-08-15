---
title: "SHT4x: A Register-Free Temperature/Humidity Sensor for Air-Quality Nodes"
date: 2026-08-15
track: iot-embedded
summary: "Sensirion's SHT4x family reaches ±1.0 %RH on the SHT45 and speaks a single-command Inter-Integrated Circuit (I2C) protocol that needs no vendor library: one command byte, six bytes back, two CRC-8 checks. This article walks the protocol, the on-chip heater that recovers the element from condensation and creep, and why a discrete sensor outside the enclosure's warm zone reads differently from the one inside a particulate monitor."
reading_time: 6
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

**Gist.** An air-quality node needs relative humidity (RH) and temperature that describe the room, not the inside of its own enclosure, and it needs them from a part that survives brownouts without a configuration state to restore. Sensirion's **SHT4x** family answers with a factory-calibrated die and a stateless Inter-Integrated Circuit (I2C) protocol: a single command byte selects a one-shot conversion, six bytes come back with two cyclic redundancy check (CRC-8) bytes embedded, and no registers persist between transactions. The cost is that recovery from condensation and long-term drift is delegated to an on-chip heater whose use the datasheet caps at **10 % duty cycle over the sensor's life**, and that a sensor placed inside a fan-and-laser enclosure reports its own self-heating rather than ambient conditions.

## Accuracy classes

The family shares one die across three bins. All variants share the protocol, the package, and a **1.08–3.6 V** supply; the price difference buys tighter calibration.

| Part | Typ. RH accuracy | Typ. T accuracy | Fit |
|---|---|---|---|
| **SHT40** | ±1.8 %RH (max ±3.5) | ±0.2 °C | General telemetry |
| **SHT41** | ±1.8 %RH (max ±2.5) | ±0.2 °C | Same typical figure, tighter guaranteed bound |
| **SHT45** | ±1.0 %RH | ±0.1 °C | Reference node, dew-point computation |

Average supply current is approximately **0.4 µA** at one low-precision measurement per second. The part draws nanoamps outside the few milliseconds of conversion, so a duty-cycled node's sensor budget is dominated by the conversion itself rather than by idle draw. Dew point and absolute humidity are nonlinear functions of both RH and temperature, so their errors compound the two input errors; the SHT45's ±1.0 %RH is the class worth paying for on whichever node is treated as ground truth.

## Protocol: one command, six bytes, two CRCs

Standard parts respond at I2C address **0x44**; the B and C variants occupy **0x45** and **0x46**, permitting three devices on one bus segment. **There is no register map.** The master writes a single command byte, waits out the conversion, then reads six bytes: temperature most-significant byte, temperature least-significant byte, CRC over those two, then the same triple for humidity.

- `0xFD` — measure, **high precision** (≤ 8.3 ms)
- `0xF6` — medium precision (≤ 4.5 ms); `0xE0` — low precision (≤ 1.6 ms)
- `0x89` — read serial number; `0x94` — soft reset

Each check byte is **CRC-8 with polynomial 0x31 and initial value 0xFF**, computed over the two preceding data bytes. Verification is load-bearing rather than ceremonial: an I2C bus routed near the brushless fan of a particulate-matter (PM) sensor will occasionally take a corrupted transfer, and **because the conversion is a linear map of the raw 16-bit word, a flipped high-order bit decodes to a well-formed number rather than to anything the caller can recognise as wrong** — a silent excursion unless the CRC is checked and the sample discarded.

Conversion from the raw 16-bit signals S<sub>T</sub> and S<sub>RH</sub> is affine and fixed-point friendly: T = −45 + 175·S<sub>T</sub>/65535 °C and RH = −6 + 125·S<sub>RH</sub>/65535 %. The RH transfer function deliberately extends below 0 % and above 100 %, so **the raw value can legitimately decode outside the physical range near saturation and must be clipped after conversion**, not rejected before it.

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
    *rh = h < 0.0f ? 0.0f : (h > 100.0f ? 100.0f : h);
    return ESP_OK;
}
```

That listing is the entire driver. The absence of an initialisation sequence has a concrete consequence for reliability: **there is no device-side configuration that a brownout can leave half-written**, so a reset firmware and a reset sensor cannot disagree about mode. The only ordering invariant the master must hold is that the read follows the command by at least the precision level's conversion time; reading early yields a bus error rather than a quietly wrong value, because the sensor does not acknowledge the read address until the conversion completes.

## The heater: recovering from condensation and creep

Polymer humidity elements exhibit two distinct degradations. **Creep** is an upward offset that accumulates when the element is held at high humidity for weeks. **Condensation** pins the reading near 100 %RH until liquid water leaves the element, which under still air can take far longer than the event that caused it.

The SHT4x provides an on-chip heater addressed by six commands spanning **20 mW, 110 mW and 200 mW at pulse lengths of 0.1 s and 1 s** — for example `0x39` selects 200 mW for 1 s. Each heater command drives the pulse and returns a measurement taken at the end of it. The datasheet bounds usage at **at most 10 % heater duty cycle over the sensor's lifetime** — total heater-on time no more than a tenth of the part's life — and specifies the heater for ambient temperatures **up to 65 °C**.

The state machine matters for the data pipeline. The measurement returned by a heater command is taken while the die is still hot, so it is **not a report of ambient conditions**; its use is diagnostic — a check that the element has dried — and it must be excluded from any logged series or averaged statistic. After the pulse, the die requires time to return to ambient before an ordinary `0xFD` reading is trustworthy again. The failure mode of ignoring this is a periodic warm/dry spike in the recorded series that correlates exactly with the heater schedule and is easily misread as a real environmental event.

## Placement: a discrete sensor versus the one inside the PM enclosure

All-in-one particulate modules such as the SEN5x report RH and temperature as well, which raises the question of whether a discrete part earns its place. The answer follows from where each sensor sits. The integrated element is **inside an enclosure containing a fan, a laser and the ESP32**, and therefore reads above ambient by whatever that enclosure's self-heating amounts to. Vendor firmware applies a compensation, but that compensation is derived for the vendor's reference enclosure, not for an arbitrary third-party one; no published figure covers the residual offset in a housing the vendor never characterised.

Temperature error propagates directly into relative humidity, because RH is the ratio of actual vapour pressure to the saturation vapour pressure at the measured temperature, and saturation pressure rises steeply with temperature — near 20 °C by roughly 6 % of its own value per °C. At an unchanged vapour content, a temperature reading 2 °C too high therefore divides the true RH by about 1.13: a room at 50 %RH is reported near **44 %RH**, an error several times the SHT45's calibration bound. Any dew-point output inherits both errors. A discrete SHT4x places a measurement point outside the warm zone and in ambient airflow, and provides a second independent element whose divergence from the integrated one over months is itself the drift signal.

Placement constraints dominate the accuracy class in practice. Mounting the part on a stub or daughterboard away from the ESP32 and the regulator, or isolating its footprint with **milled slots in the printed circuit board** — copper pour is an efficient thermal path from nearby dissipating parts to the sensor — addresses the largest error term. The enclosure requires vent slots for actual air exchange, the part must be kept out of direct sunlight and away from the PM fan exhaust, and conformal coating must not cover the membrane opening.

## Pitfalls

- **Unchecked CRC bytes.** A transfer corrupted by fan-motor noise decodes into a plausible-looking temperature and humidity, so the series shows isolated excursions with no physical cause; the two CRC-8 bytes are present precisely to reject these and cost two polynomial evaluations.
- **Reading before the conversion window elapses.** Issuing the read fewer than 8.3 ms after `0xFD` produces a bus error rather than a value, because the sensor withholds acknowledgement until conversion completes — a driver that treats the error as fatal will report the sensor as absent.
- **Logging the heater command's measurement.** The value returned at the end of a heater pulse is taken on a hot die, so a heater schedule shows up in the recorded series as a periodic warm, dry spike that resembles a real ventilation event.
- **Exceeding the heater duty-cycle bound.** The datasheet caps heater use at 10 % duty cycle over the sensor's life and specifies the heater only up to 65 °C ambient; a fixed aggressive schedule silently consumes that budget.
- **Rejecting out-of-range raw values.** The RH transfer function extends past 0 % and 100 %, so a valid CRC-checked sample near saturation can decode above 100 %; discarding it rather than clipping removes exactly the samples taken during the condensation events worth detecting.
- **Copper pour under the footprint.** Without milled slots or physical separation, the sensor reports the board's dissipation, and the offset tracks ESP32 radio duty cycle rather than the room.
- **Trusting the integrated module's compensated RH as ambient.** Its correction is calibrated for the vendor's reference enclosure; in a different housing the residual self-heating offset propagates into RH and dew point with no indication in the reported value.
