---
title: "Reading an analog sensor on the ESP32: oneshot ADC + calibration to real volts"
date: 2026-07-30
track: iot-embedded
summary: "The raw ESP32 ADC is nonlinear and part-to-part inconsistent — a raw count of 2048 is not reliably half of full scale. ESP-IDF's calibration driver turns raw counts into millivolts using per-chip eFuse data. Here's the modern oneshot + cali API, why attenuation matters, and a dependency-free reading of an analog air-quality sensor."
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

If you've ever fed an analog gas sensor or a photodiode into an ESP32 and gotten numbers that were *close* but drifted between boards, the ADC is why. The ESP32's SAR ADC is genuinely nonlinear, and the transfer curve varies chip-to-chip. A raw reading of 2048 out of 4095 is *not* dependably 1.65 V. Espressif's fix is the **calibration driver**, which reads factory-programmed correction data out of the chip's eFuses and converts raw counts into millivolts you can trust. Here's the modern (ESP-IDF v5.x) way to do it.

## Two drivers: oneshot for readings, cali for meaning

ESP-IDF splits this into two APIs you use together. The **oneshot driver** takes single on-demand samples (as opposed to the continuous/DMA driver for high-rate streaming). The **calibration driver** turns the raw count into a voltage. You set up both once, then loop.

First, get attenuation right, because it defines your input range. The ADC measures relative to an internal reference, and attenuation scales the usable window:

| Attenuation | Approx. usable input range (ESP32) |
|---|---|
| `ADC_ATTEN_DB_0` | 100–950 mV |
| `ADC_ATTEN_DB_2_5` | 100–1250 mV |
| `ADC_ATTEN_DB_6` | 150–1750 mV |
| `ADC_ATTEN_DB_12` | 150–2450 mV |

For a sensor that swings across most of 0–3.3 V you want `ADC_ATTEN_DB_12` (this is the value formerly named `ADC_ATTEN_DB_11`, renamed in ESP-IDF 5.2 — use `DB_12`). Pick the *smallest* attenuation that still covers your signal's range: less attenuation means finer resolution over that window. Note the top end still clips below 3.3 V, so scale your sensor or add a divider if it can rail.

## The setup and read loop

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

    // Calibration scheme is chip-dependent: line fitting on classic ESP32,
    // curve fitting on S2/S3/C3/C6. Create whichever this target supports.
    adc_cali_line_fitting_config_t cali_cfg = {
        .unit_id  = ADC_UNIT_1,
        .atten    = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    ESP_ERROR_CHECK(adc_cali_create_scheme_line_fitting(&cali_cfg, &cali));
}

int read_sensor_mv(void) {
    int raw = 0, mv = 0;
    ESP_ERROR_CHECK(adc_oneshot_read(adc, SENSOR_CHANNEL, &raw));
    ESP_ERROR_CHECK(adc_cali_raw_to_voltage(cali, raw, &mv));   // raw -> millivolts
    return mv;   // calibrated, per-chip corrected
}
```

`adc_cali_raw_to_voltage` is doing the real work: applying the per-chip correction so `mv` is a value you can convert to a gas concentration or lux with the sensor's datasheet formula, and expect the same answer on the next board off the reel.

## Line fitting vs. curve fitting — pick by chip

The two calibration schemes matter and are not interchangeable:

- **Line fitting** (`adc_cali_create_scheme_line_fitting`) — used on the classic **ESP32**. It corrects with a reference-voltage point (from eFuse, or a default ~1100 mV if the chip was never characterized).
- **Curve fitting** (`adc_cali_create_scheme_curve_fitting`) — used on **ESP32-S2/S3/C3/C6**. It fits a higher-order curve and is more accurate across the range.

If `adc_cali_create_scheme_*` returns `ESP_ERR_NOT_SUPPORTED`, the chip lacks eFuse calibration data — you fall back to using raw counts with a caveat, or do your own two-point calibration against a known voltage. Always check the return value; don't assume calibration succeeded.

## Two habits that clean up noisy analog readings

Even calibrated, a single sample of a slow analog sensor is jittery. Two cheap fixes:

- **Oversample and average.** Take 16–64 raw reads, average them, *then* convert to millivolts. This knocks down the ADC's random noise by √N without any extra hardware.
- **Mind ADC2 and Wi-Fi.** On the classic ESP32, ADC2 shares hardware with the Wi-Fi radio, so ADC2 reads fail while Wi-Fi is active. For any always-connected air-quality node, **put your sensor on an ADC1 channel** (GPIO32–39) and avoid the problem entirely.

Together these turn a wobbly `analogRead()`-style number into a stable, physically meaningful voltage — which is what you need before any of the sensor's datasheet math will give you a trustworthy PPM or µg/m³.

**Try next:** Wire a 10 kΩ pot across 3V3/GND into GPIO34, run the loop above, and compare `adc_cali_raw_to_voltage` output against a multimeter across the pot's full sweep — then delete the calibration call and read raw × (3300/4095) instead, and watch the error grow near the ends of the range. That divergence is exactly the nonlinearity the eFuse data is correcting.
