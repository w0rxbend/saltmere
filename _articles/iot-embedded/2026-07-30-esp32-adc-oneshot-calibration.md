---
title: "Reading an analog sensor on the ESP32: oneshot ADC and calibration to real volts"
date: 2026-07-30
track: iot-embedded
summary: "The raw ESP32 ADC is nonlinear and part-to-part inconsistent — a raw count of 2048 is not reliably half of full scale. ESP-IDF's calibration driver converts raw counts into millivolts using per-chip eFuse data. This article covers the oneshot plus calibration API, the role of attenuation, and a dependency-free reading of an analog air-quality sensor."
reading_time: 6
tags: [esp32, adc, esp-idf, calibration, analog, air-quality]
sources:
  - title: "Analog to Digital Converter (ADC) Oneshot Mode Driver — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/peripherals/adc_oneshot.html"
  - title: "Analog to Digital Converter (ADC) Calibration Driver — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/peripherals/adc_calibration.html"
  - title: "esp-idf/examples/peripherals/adc/oneshot_read — Espressif (GitHub)"
    url: "https://github.com/espressif/esp-idf/tree/master/examples/peripherals/adc/oneshot_read"
  - title: "ESP32(-S3) ADC1 ESP-IDF minimal oneshot example with curve calibration — TechOverflow"
    url: "https://techoverflow.net/2024/01/21/esp32-s3-adc1-esp-idf-minimal-oneshot-example-with-curve-calibration/"
---

**Gist.** The successive-approximation register (SAR) analog-to-digital converter (ADC) on the ESP32 has a nonlinear transfer curve that varies from chip to chip, so a raw count of 2048 out of 4095 does not dependably correspond to 1.65 V and two boards running identical firmware disagree on the same input. ESP-IDF (Espressif IoT Development Framework) addresses this with a **calibration driver** that reads factory-programmed correction data out of the chip's eFuses and maps a raw count to millivolts. The cost is that calibration is **per chip, per ADC unit, per attenuation setting and per bit width**: the correction is only valid for the exact configuration it was created with, and on parts without eFuse calibration data the scheme cannot be created at all.

## Two drivers, used together

ESP-IDF separates acquisition from interpretation. The **oneshot driver** (`esp_adc/adc_oneshot.h`) takes single on-demand samples; the continuous, direct-memory-access (DMA) driver exists separately for high-rate streaming. The **calibration driver** (`esp_adc/adc_cali.h`) converts a raw count into a voltage. Both are configured once at start-up and then used inside the sampling loop, and **the configuration must agree between them** — the attenuation and bit width passed to `adc_oneshot_config_channel` are the same values passed into the calibration scheme configuration, because the correction data is indexed by those parameters.

Attenuation is chosen first, since it fixes the usable input window. The ADC measures against an internal reference, and the attenuator scales the input into that reference's range:

| Attenuation | Approximate usable input range (ESP32) |
|---|---|
| `ADC_ATTEN_DB_0` | 100–950 mV |
| `ADC_ATTEN_DB_2_5` | 100–1250 mV |
| `ADC_ATTEN_DB_6` | 150–1750 mV |
| `ADC_ATTEN_DB_12` | 150–2450 mV |

A sensor swinging across most of 0–3.3 V requires `ADC_ATTEN_DB_12`. That identifier was named `ADC_ATTEN_DB_11` before ESP-IDF 5.2, where it was deprecated in favour of `ADC_ATTEN_DB_12`; the older name is the one found in pre-5.2 code. The selection rule is **the smallest attenuation whose window still contains the whole signal**, because the same 12-bit code space is spread over a narrower voltage window and each least-significant bit therefore covers fewer millivolts. Note the two boundaries of the table: the usable window **does not start at 0 mV and does not reach 3.3 V**. A signal that rails to the supply saturates the converter well before the supply rail, so a sensor capable of railing needs a resistive divider or a scaled front end.

## Setup and read loop

```c
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

#define SENSOR_CHANNEL  ADC_CHANNEL_6   // GPIO34 on ESP32 (ADC1)

static adc_oneshot_unit_handle_t adc;
static adc_cali_handle_t cali;

void adc_setup(void) {
    adc_oneshot_unit_init_cfg_t unit_cfg = { .unit_id = ADC_UNIT_1 };
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&unit_cfg, &adc));

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten   = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,   // 12-bit on ESP32
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc, SENSOR_CHANNEL, &chan_cfg));

    // Scheme is chip-dependent: line fitting on classic ESP32,
    // curve fitting on S2/S3/C3/C6. Fields must match chan_cfg above.
    adc_cali_line_fitting_config_t cali_cfg = {
        .unit_id  = ADC_UNIT_1,
        .atten    = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    ESP_ERROR_CHECK(adc_cali_create_scheme_line_fitting(&cali_cfg, &cali));
}
```

The read path is two calls, one per driver:

```c
int read_sensor_mv(void) {
    int raw = 0, mv = 0;
    ESP_ERROR_CHECK(adc_oneshot_read(adc, SENSOR_CHANNEL, &raw));
    ESP_ERROR_CHECK(adc_cali_raw_to_voltage(cali, raw, &mv));   // raw -> millivolts
    return mv;
}
```

`adc_cali_raw_to_voltage` carries the per-chip correction. Its output is the quantity a datasheet formula expects: a voltage that can be turned into a gas concentration or an illuminance and that stays comparable across units built from different chips. Without it, the naive conversion `raw * 3300 / 4095` assumes both a perfectly linear converter and a reference voltage identical on every part, and neither assumption holds.

## Line fitting against curve fitting

The two calibration schemes are not interchangeable; **the target chip determines which one exists**.

- **Line fitting** (`adc_cali_create_scheme_line_fitting`) is the scheme on the classic **ESP32**. It corrects using calibration data in eFuse — either a stored reference voltage or a two-point measurement — and its configuration struct exposes a `default_vref` field used when the chip carries neither.
- **Curve fitting** (`adc_cali_create_scheme_curve_fitting`) is the scheme on later parts including **ESP32-S2, S3, C3 and C6**. It fits a curve rather than applying a single-point correction.

Firmware that must build for more than one target selects the scheme by compile-time chip macro rather than assuming one is present. When `adc_cali_create_scheme_*` returns `ESP_ERR_NOT_SUPPORTED`, **the chip lacks the eFuse calibration data the scheme needs**. The remaining options are to report raw counts with that limitation stated, or to perform a two-point calibration in the application against a known external voltage. The return value is load-bearing: a handle left uninitialised by a failed create call cannot be passed to `adc_cali_raw_to_voltage`.

## Reducing noise on a slow sensor

Calibration removes systematic error, not random error. A single sample of a slow analog sensor still jitters, and two measures address that without additional hardware.

**Oversample in the raw domain.** Take a batch of raw reads — 16 to 64 for a slow sensor — average them, and convert the average once. Averaging N independent samples reduces the standard deviation of zero-mean random noise by a factor of √N. The averaging must happen **before** the conversion to millivolts, not after, so that the correction is applied once to a settled value.

**Keep the sensor off ADC2 on the classic ESP32.** On that part, **ADC2 shares hardware with the Wi-Fi radio, and ADC2 reads fail while Wi-Fi is active**. The failure is a returned error from the read call, not a silently wrong number, but for a permanently connected node it means the channel is effectively unavailable. Placing the sensor on an ADC1 channel — GPIO32 through GPIO39 on the ESP32 — removes the conflict rather than working around it.

**Verification.** Wiring a 10 kΩ potentiometer across 3V3 and ground into GPIO34 and comparing `adc_cali_raw_to_voltage` output against a multimeter sweeps the input across the attenuation window, above which the reading saturates. Replacing the calibration call with `raw * 3300 / 4095` and repeating the sweep shows the error growing near the ends of the range; that divergence is the nonlinearity the eFuse data corrects.

## Pitfalls

- **Attenuation mismatch between the channel and the calibration scheme.** Readings are consistently offset or scaled wrong while every call returns `ESP_OK`, because the correction was built for a different input window than the one the channel is sampling.
- **A shared calibration handle across channels with different attenuation.** Same symptom, same cause: the handle encodes one attenuation and one bit width, so a second channel configured differently needs its own handle.
- **Ignoring the return of `adc_cali_create_scheme_*`.** On a chip without eFuse calibration data the create call fails with `ESP_ERR_NOT_SUPPORTED` and leaves no valid handle; the subsequent conversion call then operates on an uninitialised handle.
- **ADC2 reads on the classic ESP32 with Wi-Fi up.** Reads fail once the radio is active, so a sensor that worked on the bench stops reporting as soon as the node associates.
- **A sensor allowed to reach the supply rail.** The signal clips at the top of the attenuation window, which sits below 3.3 V, so the reading saturates while the physical quantity is still rising.
- **Averaging after conversion instead of before.** The correction is applied to each noisy sample individually, which wastes the oversampling and mixes per-sample nonlinearity into the mean.
- **Carrying `ADC_ATTEN_DB_11` into ESP-IDF 5.2 or later.** The identifier was deprecated in favour of `ADC_ATTEN_DB_12`, so pre-5.2 code builds with a deprecation warning against the newer header.
