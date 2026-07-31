---
title: "ESP32-C5: Espressif's First Dual-Band Wi-Fi 6 RISC-V SoC"
date: 2026-07-31
track: iot-embedded
summary: "A RISC-V ESP32 that finally speaks 5 GHz Wi-Fi 6 — what's on the die, why the second band matters for crowded 2.4 GHz sensor fleets, and how to build for it with ESP-IDF."
reading_time: 5
tags: [esp32, esp32-c5, wifi6, risc-v, esp-idf, 802-15-4]
sources:
  - title: "ESP32-C5 2.4 and 5 GHz Dual-band Wi-Fi 6 MCU (Espressif product page)"
    url: "https://www.espressif.com/en/products/socs/esp32-c5"
  - title: "Espressif's ESP32-C5 is Now in Mass Production (Espressif news)"
    url: "https://www.espressif.com/en/news/ESP32-C5_Mass_Production"
  - title: "Wi-Fi Driver — ESP32-C5, ESP-IDF Programming Guide v5.5.4"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.5.4/esp32c5/api-guides/wifi.html"
  - title: "ESP32-C5 dual-band Wi-Fi 6 SoC enters mass production (CNX-Software)"
    url: "https://www.cnx-software.com/2025/04/30/esp32-c5-mass-production-esp32-c5-devkitc-1-board/"
---

For years the answer to "which ESP32 does 5 GHz Wi-Fi?" was "none of them." Every part in the lineup — original ESP32, the C3, the S3 — was a 2.4 GHz-only radio sharing spectrum with every phone, laptop, microwave, and Zigbee bulb in the building. The **ESP32-C5** ends that. It's Espressif's first SoC with **dual-band 2.4 GHz *and* 5 GHz Wi-Fi 6**, and it reached **mass production on 30 April 2025**.

## What's actually on the die

The C5 is deliberately a single-core RISC-V part, not a dual-core S-class chip:

- **CPU:** one 32-bit RISC-V core up to **240 MHz**, plus a separate **low-power (LP) core up to 40 MHz** for wake-on-event duty.
- **Memory:** **384 KB SRAM**, 320 KB ROM, plus external flash and PSRAM support.
- **Wi-Fi 6 (802.11ax) on both bands**, backward-compatible down to 802.11a/b/g/n/ac.
- **Bluetooth 5 (LE)** with coded PHY and 2 Mbps high throughput.
- **IEEE 802.15.4** radio for **Thread, Zigbee 3.0, and Matter**.
- Up to 29 GPIOs, SDIO and QSPI high-speed interfaces, plus the usual TEE, secure boot, and flash/PSRAM encryption.

So on one die you get Wi-Fi 6, BLE, and an 802.15.4 mesh radio — a genuine single-chip Matter border-router-class device. Espressif's own ESP32-C5-DevKitC-1 landed around $15.

## Why the second band matters

The interesting spec isn't Wi-Fi 6's OFDMA or TWT (though TWT is genuinely useful for battery sensors). It's the **5 GHz band itself**.

If you run air-quality nodes in an apartment block or an office, 2.4 GHz is a swamp: only three non-overlapping 20 MHz channels (1, 6, 11), shared with Bluetooth, Zigbee, and every neighbour's router. Retries climb, latency gets spiky, and a fleet of chatty sensors makes it worse for everyone. 5 GHz offers **far more non-overlapping channels** and dramatically less contention. Being able to move telemetry-heavy nodes — or a local dashboard uplink — to 5 GHz while leaving low-rate nodes on 2.4 GHz is a real deployment lever you simply didn't have on prior ESP32s. The trade-off is the usual physics: 5 GHz has shorter range and worse wall penetration, which is exactly why keeping *both* bands on one part is the point.

## ESP-IDF support

The `esp32c5` build target first appeared as a preview during the **v5.4** development cycle. For the mass-production silicon, use **ESP-IDF v5.5 or newer**. Update your local checkout before you start:

```bash
cd ~/esp/esp-idf
git checkout v5.5.4          # or a later v5.5.x / v6.x
git submodule update --init --recursive
./install.sh esp32c5
. ./export.sh
```

Then set the target and flash an example — the bones of any network job, `wifi/getting_started/station`:

```bash
cd examples/wifi/getting_started/station
idf.py set-target esp32c5
idf.py menuconfig          # set SSID/password under "Example Configuration"
idf.py -p /dev/ttyACM0 flash monitor
```

## Choosing the 5 GHz band

Band selection is a global driver setting, separate from `wifi_config_t`. Call `esp_wifi_set_band_mode()` after init and before connecting:

```c
#include "esp_wifi.h"

ESP_ERROR_CHECK(esp_wifi_init(&cfg));
ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

// Force the 5 GHz band on a congested 2.4 GHz site.
// Options: WIFI_BAND_MODE_2G_ONLY, WIFI_BAND_MODE_5G_ONLY, WIFI_BAND_MODE_AUTO
ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_5G_ONLY));

ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
ESP_ERROR_CHECK(esp_wifi_start());
```

Leave it on `WIFI_BAND_MODE_AUTO` and the driver scans and picks per your AP's availability; pin it to `5G_ONLY` when you've deliberately parked an AP on 5 GHz to escape the 2.4 GHz crowd. Remember your access point must actually broadcast the SSID on 5 GHz — a `5G_ONLY` node will never see a 2.4 GHz-only network.

The practical upshot: for the first time you can architect an ESP32 sensor fleet that isn't hostage to 2.4 GHz congestion, without changing MCU families or losing BLE and Thread.

**Try next:** flash the `station` example twice — once with `WIFI_BAND_MODE_2G_ONLY`, once with `5G_ONLY` — and compare RSSI and ping jitter from the same spot to see what your site's 5 GHz headroom actually buys you.
