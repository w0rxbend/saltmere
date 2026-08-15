---
title: "ESP32-C5: Espressif's First Dual-Band Wi-Fi 6 RISC-V SoC"
date: 2026-07-31
track: iot-embedded
summary: "A RISC-V ESP32 that speaks 5 GHz Wi-Fi 6: what is on the die, what the second band changes for crowded 2.4 GHz sensor fleets, and how band selection is expressed in ESP-IDF."
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

**Gist.** Every ESP32 part before the C5 — the original ESP32, the C3, the S3 — carried a 2.4 GHz-only radio, confining entire sensor fleets to a band shared with Bluetooth, IEEE 802.15.4 traffic and neighbouring access points. The **ESP32-C5** adds a second radio band: it is Espressif's first system-on-chip (SoC) with **dual-band 2.4 GHz and 5 GHz Wi-Fi 6 (IEEE 802.11ax)**, in **mass production since 30 April 2025**. The cost of moving a node to 5 GHz is physical rather than architectural — shorter range and worse penetration through walls — which is the reason a dual-band part, rather than a 5 GHz-only part, is the useful shape.

## What is on the die

The C5 is a single-core RISC-V part rather than a dual-core S-class chip. The documented configuration:

- **Central processing unit (CPU):** one 32-bit RISC-V core at up to **240 MHz**, plus a separate **low-power (LP) core** for wake-on-event duty.
- **Memory:** **384 KB of static RAM (SRAM)** and **320 KB of ROM**, with support for external flash and pseudo-static RAM (PSRAM).
- **Wi-Fi 6 (802.11ax) on both 2.4 GHz and 5 GHz**, backward-compatible with 802.11a/b/g/n/ac.
- **Bluetooth 5 Low Energy (LE)**, including the coded physical layer (PHY) and the 2 Mbps high-throughput PHY.
- An **IEEE 802.15.4 radio**, the link layer under **Thread, Zigbee 3.0 and Matter**.
- General-purpose input/output pins (GPIOs), SDIO and quad SPI (QSPI) high-speed interfaces, and a trusted execution environment (TEE) with secure boot and flash/PSRAM encryption.

The combination that matters is the co-residency of three radios on one die: **Wi-Fi 6, Bluetooth LE and 802.15.4**. That places a single-chip Matter border-router-class device within reach, where earlier designs required a second radio module. Espressif's reference board for the part is the ESP32-C5-DevKitC-1.

## What the second band changes

The consequential specification is not orthogonal frequency-division multiple access (OFDMA) or target wake time (TWT) — although TWT is directly applicable to battery-powered sensors, since it schedules a station's wake windows rather than leaving it to poll. The consequential specification is **the 5 GHz band itself**.

The structural problem in 2.4 GHz is channel count. The band offers **three non-overlapping 20 MHz channels (1, 6 and 11)**, and those three are shared with Bluetooth, with 802.15.4 mesh traffic, and with every access point in radio range. Contention on a shared channel is resolved by carrier-sense multiple access with collision avoidance (CSMA/CA): a station that senses the medium busy defers, and a collision triggers a retransmission after a backoff. The observable consequences are a rising retry count and **latency whose variance grows faster than its mean** — a fleet of chatty sensors degrades the band for itself and for every other occupant.

The 5 GHz band provides **substantially more non-overlapping channels**, so the same traffic is spread across more independent contention domains. The counterweight is propagation: **5 GHz signals attenuate more sharply and penetrate walls less well than 2.4 GHz**. Dual-band silicon converts that into a per-node placement decision rather than a fleet-wide one — telemetry-heavy nodes or a dashboard uplink on 5 GHz, low-rate nodes at the edge of coverage left on 2.4 GHz — without changing microcontroller family and without giving up Bluetooth LE or Thread.

## ESP-IDF support

The `esp32c5` build target first appeared as a preview during the **v5.4** development cycle. Mass-production silicon requires **ESP-IDF v5.5 or newer**. Preparing a local checkout:

```bash
cd ~/esp/esp-idf
git checkout v5.5.4          # or a later v5.5.x / v6.x
git submodule update --init --recursive
./install.sh esp32c5
. ./export.sh
```

`install.sh esp32c5` fetches the RISC-V toolchain for this target specifically; a checkout installed for another target will not build `esp32c5`. Setting the target and flashing the station example, which is the skeleton of any networked firmware:

```bash
cd examples/wifi/getting_started/station
idf.py set-target esp32c5
idf.py menuconfig          # SSID and password under "Example Configuration"
idf.py -p /dev/ttyACM0 flash monitor
```

`idf.py set-target` regenerates the build directory and resets `sdkconfig` to the defaults for the new target, so target selection precedes any `menuconfig` edits that must survive.

## Selecting the band

Band selection is a **global driver setting, distinct from `wifi_config_t`** — it is not a per-interface field carried alongside the SSID and credentials. It is applied with `esp_wifi_set_band_mode()` **after `esp_wifi_init()` and before the connection attempt**:

```c
#include "esp_wifi.h"

ESP_ERROR_CHECK(esp_wifi_init(&cfg));
ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));

// Pin the radio to 5 GHz on a congested 2.4 GHz site.
// Options: WIFI_BAND_MODE_2G_ONLY, WIFI_BAND_MODE_5G_ONLY, WIFI_BAND_MODE_AUTO
ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_5G_ONLY));

ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta_cfg));
ESP_ERROR_CHECK(esp_wifi_start());
```

The three modes differ in which bands the driver will scan. Under `WIFI_BAND_MODE_AUTO` the driver scans and selects according to what the access point offers. Under `WIFI_BAND_MODE_5G_ONLY` the 2.4 GHz band is excluded from scanning entirely, which is the intended configuration when an access point has been deliberately parked on 5 GHz to escape 2.4 GHz contention. **The invariant this imposes is that the access point must broadcast the SSID on the pinned band**: a station in `5G_ONLY` will never observe a 2.4 GHz-only network, and the failure presents as a station that scans and never associates rather than as an error at the point of configuration.

An empirical way to size the benefit on a specific site: flash the `station` example twice from the same physical position, once with `WIFI_BAND_MODE_2G_ONLY` and once with `WIFI_BAND_MODE_5G_ONLY`, and compare received signal strength indication (RSSI) and ping jitter. The comparison measures that site's 5 GHz headroom, which is a property of the building and its neighbours, not of the silicon.

## Pitfalls

- **`WIFI_BAND_MODE_5G_ONLY` against a 2.4 GHz-only or single-SSID-on-2.4 access point:** the station scans indefinitely and never associates. `esp_wifi_set_band_mode()` returns success; the band is excluded from scanning, so the network is invisible rather than rejected.
- **Calling `esp_wifi_set_band_mode()` after the connection attempt:** the setting is global driver state applied at init time, so a call issued once association is under way does not retroactively change the band the connection was made on.
- **Assuming 5 GHz coverage matches the 2.4 GHz survey:** 5 GHz attenuates more sharply and penetrates walls less well, so a node that associated reliably on 2.4 GHz from a given position may sit below usable RSSI on 5 GHz from the same position.
- **Treating the C5 as a drop-in for a dual-core S-series part:** it is single-core RISC-V with 384 KB of SRAM, so firmware sized against an S3's core count or memory budget does not transfer unmodified.
- **Building with a pre-v5.5 ESP-IDF:** the `esp32c5` target existed only as a preview during the v5.4 cycle, and mass-production silicon requires v5.5 or newer.
- **Running `idf.py menuconfig` before `idf.py set-target esp32c5`:** the target switch regenerates `sdkconfig` from the new target's defaults, discarding the earlier edits.
