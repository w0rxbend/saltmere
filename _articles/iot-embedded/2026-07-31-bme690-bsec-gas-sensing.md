---
title: "BME690: raw gas resistance on ESP-IDF, and where BSEC3 fits"
date: 2026-07-31
summary: "The BME690 succeeds the BME688 with the same four-in-one gas, temperature, pressure and humidity stack, hardened for condensation and drawing less power in indoor-air-quality modes. This covers what changed, how the raw driver, BSEC3 and AI Studio divide the work, and a forced-mode read on ESP-IDF using Bosch's official C driver."
track: iot-embedded
reading_time: 6
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

**Gist.** A metal-oxide (MOX) gas sensor reports a resistance that varies with volatile-organic-compound (VOC) load, but also with humidity, temperature, plate history and long-term drift, so a bare ohm reading carries no absolute meaning. The BME690 pairs that MOX hot-plate with compensated temperature, pressure and humidity channels, and Bosch's BSEC fusion library converts the raw resistance plus humidity history into an indoor-air-quality (IAQ) index by tracking a baseline. The cost is a layered, version-coupled stack: the raw driver alone gives no index, BSEC imposes a prescribed sample interval and a warm-up before its output is meaningful, and classifier models trained in BME AI-Studio are tied to the board they were recorded on.

## What the 690 changes relative to the 688

The BME690 is an incremental physical revision, not a new sensing category. It keeps the package and the measurand set of the BME688: a 3.0 x 3.0 x 0.93 mm eight-pin land-grid-array (LGA) part combining gas, temperature, pressure and humidity, with a MOX hot-plate driving the gas channel.

- **Robustness under condensation.** Bosch positions the 690 as reliable in high-condensation environments, where the 688's gas plate is less dependable. This matters for outdoor enclosures, bathrooms, greenhouses and cold-start cabins — anywhere the dew point is crossed repeatedly.
- **Lower power in IAQ modes.** The BSEC air-quality duty cycles draw less energy on the 690, which is the binding constraint for battery-powered air-quality nodes.
- **Same measurands.** Gas resistance responds to VOCs and **volatile sulfur compounds (VSCs)**. Temperature spans -40…85 °C, quoted at ±0.5 °C over 0–65 °C; humidity 0–100 %RH at ±3 %; pressure 300–1100 hPa at ±0.5 hPa absolute. The inter-integrated-circuit (I2C) bus runs up to 3.4 MHz; serial peripheral interface (SPI), three- or four-wire, up to 10 MHz.

The load-bearing non-change is addressing. The **default I2C address remains 0x76** with SDO/ADDR pulled low, and **0x77** with ADDR pulled high, identical to the 68x family, so existing wiring and bus scans carry over unmodified.

## Three layers, distinct responsibilities

1. **`BME690_SensorAPI`** — Bosch's official C driver, BSD-3-Clause. Headers are `bme69x.h` and `bme69x_defs.h`; the entry points are `bme69x_init`, `bme69x_set_conf`, `bme69x_set_op_mode` and `bme69x_get_data`. It yields **raw gas resistance in ohms** plus compensated temperature, pressure and humidity. It computes no air-quality index.
2. **BSEC** — the fusion library. It consumes raw gas resistance together with humidity history and produces an IAQ index and gas-class outputs, absorbing baseline drift so that a slowly rising or falling resistance floor does not register as an air-quality change. The version coupling is strict: **the BME690 requires BSEC 3.2.0.0 or newer.** BSEC2, bundled in the Arduino `Bosch-BSEC2-Library`, targets the BME688 and not the BME690.
3. **BME AI-Studio** — the desktop and mobile tool used to record labelled gas specimens, train a classifier (for example, clean air versus bacterial growth versus solvent), and export an algorithm blob that BSEC3 loads at runtime.

The constraint that most often invalidates work already done: **AI-Studio training data is board-specific.** Specimens recorded on a BME688 devkit cannot train a BME690 model, and the reverse also fails; the hot-plate response differs enough that the model does not transfer. A migration between the two parts therefore requires re-collecting the entire specimen dataset, not merely retraining on the existing one.

## Forced-mode read on ESP-IDF

Bringing up the raw driver first separates two failure classes that otherwise arrive together: bus and wiring faults, and fusion-library configuration faults. Espressif packages the Bosch API as a component, `espressif/bme690` v1.0.3, BSD-3-Clause, which supplies the I2C and SPI glue:

```bash
idf.py add-dependency "espressif/bme690^1.0.3"
```

Forced mode performs **one** temperature/pressure/humidity/gas conversion per trigger and returns the device to sleep, which is what makes the duty cycle explicit rather than free-running. The sequence below takes a single sample with a 300 °C heater step held for 100 ms:

```c
#include "bme69x.h"
#include "bme690_common.h"   // bme69x_interface_init() from the component

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

    // Conversion time depends on the oversampling settings above;
    // the heater interval is additional and is not included in it.
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

Two elements of that sequence are load-bearing. First, the wait is **`bme69x_get_meas_dur` plus the heater duration**: the returned conversion time reflects the oversampling configuration and does not include the plate heating interval, so omitting the addition reads the device before it has finished. Second, `n_fields` reports how many data frames were produced; a zero means no completed measurement was available and `data` must not be interpreted.

Validity is carried in `data.status`. **`BME69X_GASM_VALID_MSK` indicates the gas measurement completed, and `BME69X_HEAT_STAB_MSK` indicates the plate reached a stable target temperature**; the samples immediately following power-on typically clear the stability bit. Raw gas resistance rises in clean air and falls as VOC load increases, but only as a relative signal against an unstated baseline — which is the quantity BSEC maintains.

## From ohms to an air-quality figure

An absolute IAQ number requires BSEC 3.2.0.0 or newer, fed the raw driver output **on the sample interval BSEC prescribes for the selected configuration**; the baseline tracking is defined in terms of that cadence, so an ad-hoc polling rate changes what the index means. Gas classification — separating coffee from solvent from spoilage — follows a different path: specimens are recorded in AI-Studio, a model is trained and exported, and BSEC3 loads it through its classifier path. The deployment board revision must match the revision used for recording.

A useful first measurement is to log `gas_resistance` once per second from the forced-mode read above, exhale onto the sensor, and observe the resistance fall and then recover in clean air. The recovery trajectory, and the fact that its endpoint is not necessarily the starting value, is precisely the drift BSEC's baseline tracking exists to absorb.

## Pitfalls

- **Reading before the heater interval elapses.** `bme69x_get_data` returns `n_fields == 0`, or a frame with `BME69X_HEAT_STAB_MSK` clear, because `bme69x_get_meas_dur` covers only the conversion and not `heatr_dur`.
- **Trusting the first samples after power-on.** Gas resistance is far from its settled value because the plate has not stabilized; the condition is visible in `data.status` and is silently ignored if the mask is not checked.
- **Driving a BME690 with BSEC2.** The Arduino `Bosch-BSEC2-Library` carries a BSEC generation targeting the BME688; BME690 support begins at BSEC 3.2.0.0.
- **Reusing BME688 specimens to train a BME690 classifier.** The hot-plate response differs between the parts, so the model does not transfer and the dataset must be re-recorded on the target board.
- **Treating raw ohms as an air-quality reading.** The value is relative to an unmaintained baseline that drifts with humidity and sensor history, so identical air yields different ohms on different days.
- **Assuming address 0x77.** The default is 0x76 with SDO/ADDR low; a floating ADDR line leaves the responding address undetermined and the bus scan inconsistent between resets.
