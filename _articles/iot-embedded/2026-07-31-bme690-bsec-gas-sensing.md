---
title: "BME690: Bosch's AI gas scanner and getting a raw read on an ESP32"
date: 2026-07-31
track: iot-embedded
summary: "The BME690 is the successor to the BME688: same 4-in-1 gas/temperature/pressure/humidity stack, but hardened for condensation and lower-power in IAQ modes. Here's what it adds, how BSEC3 and AI Studio fit together, and a forced-mode read on ESP-IDF using Bosch's official driver."
reading_time: 5
tags: [bme690, bosch, gas-sensor, air-quality, esp32, esp-idf, bsec, i2c]
sources:
  - title: "Bosch Sensortec — Gas sensor BME690"
    url: "https://www.bosch-sensortec.com/en/products/environmental-sensors/gas-sensors/bme690"
  - title: "BME AI-Studio docs — Introducing BME690 (BSEC 3.2.0.0)"
    url: "https://www.bosch-sensortec.com/software/bme/docs/introducingbme690/overview.html"
  - title: "boschsensortec/BME690_SensorAPI (official C driver)"
    url: "https://github.com/boschsensortec/BME690_SensorAPI"
  - title: "espressif/bme690 — ESP Component Registry"
    url: "https://components.espressif.com/components/espressif/bme690"
  - title: "teach-your-pi-to-sniff-with-bme688 — AI Studio BME690 devkit notes"
    url: "https://github.com/mcalisterkm/teach-your-pi-to-sniff-with-bme688"
---

Bosch quietly shipped the **BME690**, the successor to the BME688. If you've built anything with the 680/688 you already know the shape: a single 3.0 x 3.0 x 0.93 mm 8-pin LGA that does gas, temperature, pressure and humidity, with a MOX hot-plate for the gas channel. The 690 keeps all of that and changes the parts that bite you in the field.

## What the 690 actually adds

It's an incremental, physical improvement, not a new sensing category:

- **Robustness in high-condensation environments.** The 688's gas plate is unhappy around dew-point cycling; the 690 is built to run reliably where moisture condenses. Relevant for outdoor enclosures, bathrooms, greenhouses, cold-start cabins.
- **Lower power in IAQ modes.** The BSEC air-quality duty cycles draw less energy, which matters for battery air-quality nodes.
- Same measurands: gas resistance (VOCs, **volatile sulfur compounds**, plus CO and H2), temperature (-40…85 °C, ±0.5 °C), humidity (0–100 %RH, ±3 %), pressure (300–1100 hPa). I2C up to 3.4 MHz, SPI 3/4-wire up to 10 MHz.

The important non-change: default I2C address is **0x76** (SDO/ADDR low), 0x77 when you pull ADDR high. Same as the 68x family, so existing wiring carries over.

## The software stack: raw driver vs BSEC3 vs AI Studio

Three layers, don't conflate them:

1. **`BME690_SensorAPI`** — Bosch's official C driver (BSD-3-Clause, v1.1.0, Feb 2026). Header is `bme69x.h` / `bme69x_defs.h`, functions are `bme69x_init`, `bme69x_set_conf`, `bme69x_set_op_mode`, `bme69x_get_data`. This gives you *raw* gas resistance in ohms plus compensated T/P/H. No air-quality index.
2. **BSEC (fusion library)** — turns raw gas resistance + humidity history into an IAQ index and gas-class outputs, handling baseline drift. Critical version note: **BME690 requires BSEC 3.2.0.0 or newer.** The older BSEC2 (bundled in the Arduino `Bosch-BSEC2-Library`, currently v1.10.2610 with BSEC ~2.6.1.0) targets the 688, not the 690.
3. **BME AI-Studio** — the desktop/mobile tool where you record labelled gas specimens, train a classifier (e.g. "clean vs bacterial growth vs solvent"), and export an algorithm blob that BSEC3 loads at runtime.

One gotcha worth burning into memory: AI Studio training data is **board-specific**. Specimens recorded on a BME688 devkit cannot train a BME690 model, and vice versa. The hot-plate response differs enough that the model won't transfer. Budget for re-collecting your dataset if you're migrating.

## A raw forced-mode read on ESP-IDF

Start with the raw driver before touching BSEC — it proves your wiring and gives you gas resistance to eyeball. Espressif packages the Bosch API as a component (`espressif/bme690`, v1.0.3, BSD-3-Clause) that wraps the I2C/SPI glue for you:

```bash
idf.py add-dependency "espressif/bme690^1.0.3"
```

Then a single forced-mode measurement (one T/P/H/gas sample with a 300 °C heater step):

```c
#include "bme69x.h"
#include "common.h"   // bme69x_interface_init() from the component

void bme690_read(void) {
    struct bme69x_dev dev;
    struct bme69x_conf conf;
    struct bme69x_heatr_conf heatr;
    struct bme69x_data data;
    uint8_t n_fields;

    // Wires up I2C read/write/delay for addr 0x76
    bme69x_interface_init(&dev, BME69X_I2C_INTF);
    bme69x_init(&dev);

    conf.os_hum  = BME69X_OS_16X;
    conf.os_temp = BME69X_OS_2X;
    conf.os_pres = BME69X_OS_1X;
    conf.filter  = BME69X_FILTER_OFF;
    conf.odr     = BME69X_ODR_NONE;
    bme69x_set_conf(&conf, &dev);

    heatr.enable    = BME69X_ENABLE;
    heatr.heatr_temp = 300;   // target plate temp, °C
    heatr.heatr_dur  = 100;   // heat time, ms
    bme69x_set_heatr_conf(BME69X_FORCED_MODE, &heatr, &dev);

    bme69x_set_op_mode(BME69X_FORCED_MODE, &dev);

    // Wait: measurement duration + heater duration
    uint32_t del_us = bme69x_get_meas_dur(BME69X_FORCED_MODE, &conf, &dev)
                      + (uint32_t)heatr.heatr_dur * 1000;
    dev.delay_us(del_us, dev.intf_ptr);

    bme69x_get_data(BME69X_FORCED_MODE, &data, &n_fields, &dev);
    if (n_fields) {
        printf("T=%.2f C  RH=%.2f %%  P=%.1f hPa  Rgas=%.0f ohm  status=0x%x\n",
               data.temperature, data.humidity, data.pressure / 100.0,
               data.gas_resistance, data.status);
    }
}
```

Check `data.status & BME69X_GASM_VALID_MSK` and `BME69X_HEAT_STAB_MSK` before trusting the gas reading — the first few samples after power-on won't have a stabilized plate. Raw gas resistance rises in clean air and drops as VOC load increases; it's a relative signal, which is exactly why BSEC exists to normalize it into an index.

## Where to go for real air quality

Raw ohms are fine for a demo, useless for a product. For an actual IAQ number, drop in BSEC3 (3.2.0.0+), feed it the raw driver output on its prescribed sample interval, and let it manage the baseline. For gas *classification* — telling coffee from solvent from spoilage — you record specimens in AI Studio, train, export the model, and load it through BSEC3's classifier path. Keep the training set on the same board revision you'll deploy.

**Try next:** flash the raw forced-mode read above to an ESP32 with a BME690 breakout on 0x76, log `gas_resistance` once a second, and breathe on it — watch the ohms drop, then confirm they recover in clean air. That baseline behavior is what BSEC3 is compensating for.
