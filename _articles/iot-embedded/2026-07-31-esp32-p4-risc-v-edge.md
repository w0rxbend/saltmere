---
title: "The ESP32-P4: a RISC-V Application Processor That Brings Its Own Wi-Fi (Sort Of)"
date: 2026-07-31
track: iot-embedded
summary: "Espressif's ESP32-P4 is a dual-core 400 MHz RISC-V SoC with a vector AI extension, MIPI display and camera interfaces, USB 2.0 High-Speed, and an H.264 encoder — but no radio. Here's what the silicon actually offers and how you get Wi-Fi and MQTT onto it by pairing it with an ESP32-C6."
reading_time: 5
tags: [esp32, esp32-p4, risc-v, edge-ai, mipi, esp-idf]
sources:
  - title: "ESP32-P4 High-performance SoC — product page (Espressif)"
    url: "https://www.espressif.com/en/products/socs/esp32-p4"
  - title: "ESP32-P4 Series Datasheet (Espressif)"
    url: "https://documentation.espressif.com/esp32-p4_datasheet_en.html"
  - title: "Espressif Reveals ESP32-P4 (announcement, Jan 5 2023)"
    url: "https://www.espressif.com/en/news/ESP32-P4"
  - title: "ESP-IDF Programming Guide — Wi-Fi Expansion (ESP32-P4)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32p4/api-guides/wifi-expansion.html"
  - title: "esp-hosted-mcu — ESP32-P4-Function-EV-Board (C6-over-SDIO setup)"
    url: "https://github.com/espressif/esp-hosted-mcu/blob/main/docs/esp32_p4_function_ev_board.md"
---

For years "ESP32" meant "a microcontroller with Wi-Fi baked in." The **ESP32-P4** breaks that assumption in both directions: it's Espressif's most capable application processor to date, and it has *no radio at all*. Announced in January 2023 and now shipping on dev boards, it's aimed at the jobs the classic ESP32 always struggled with — driving a real display, ingesting a camera, running edge vision — while leaving connectivity to a companion chip.

## What's actually on the die

The headline compute is a **dual-core 32-bit RISC-V high-performance core running up to 400 MHz** (RV32IMAFC with a single-precision FPU), plus a **third low-power RISC-V core at up to 40 MHz** for the always-on, sip-power duties. For signal and AI work there's a custom **128-bit vector (SIMD) extension** — complex multiply, add/sub, shift, compare — which is what makes on-device inference and DSP practical rather than aspirational.

Around the cores, the P4 is built for interfaces the S3 never had:

- **MIPI-DSI, 2 data lanes** and **MIPI-CSI, 2 data lanes with an integrated ISP**, both handling up to 1080p — a real display *and* a real camera, not a bit-banged parallel LCD.
- **USB 2.0 High-Speed OTG** at 480 Mbps.
- An **H.264 hardware encoder** (up to 1080p at 30 fps per Espressif's product page), so a P4 can compress video on the fly instead of shipping raw frames.
- **768 KB of high-performance L2 SRAM** plus 32 KB LP SRAM, and in-package **PSRAM options of 16 MB or 32 MB** — the Waveshare P4-Nano ships with 32 MB.

That's a chip you'd reach for to build a smart display, a doorbell camera, or an air-quality station with a live graphing UI and on-device anomaly detection — the kind of thing that used to mean adding a Linux SBC.

## The missing radio, and how you fill it

Here's the design tax: **no native Wi-Fi or Bluetooth.** Espressif's intended architecture is to pair the P4 with a wireless *companion* — an **ESP32-C6** (Wi-Fi 6 + BLE 5) or C5 — connected over SDIO, SPI, or UART. The reference boards (the ESP32-P4-Function-EV-Board and the Waveshare P4-Nano) wire a C6 to the P4 over SDIO and pre-flash it with hosted-slave firmware.

The software that makes this ergonomic is two layers: `esp_hosted` (the transport driver on both chips) and `esp_wifi_remote` (a shim so the ordinary `esp_wifi_*` and `esp-netif` calls you already know are forwarded transparently to the companion). The practical upshot is you write standard networking code and it "just works" over the link:

```bash
idf.py set-target esp32p4
idf.py add-dependency "espressif/esp_wifi_remote"
idf.py add-dependency "espressif/esp_hosted"
idf.py menuconfig       # pick the ESP32-C6 slave + SDIO transport
idf.py build flash monitor
```

Because `esp_wifi_remote` intercepts the normal Wi-Fi API, you can then build the stock ESP-IDF examples unchanged — point `examples/protocols/mqtt` at your broker and it connects through the C6 as if the radio were on the P4 itself. Target support has been in mainline ESP-IDF since the v5.3 line, with current stable docs on the v6.0.x series.

The mental model shift is worth stating plainly: the P4 turns "ESP32 project" into a *two-chip* project, a fast application core plus a small wireless coprocessor, which is exactly the split you'd design at the board level for anything doing serious local compute anyway.

**Try next:** If you have a P4 dev board with an onboard C6, flash the `esp_hosted` slave firmware to the C6, then build and run the unmodified `examples/protocols/mqtt/tcp` on the P4 with `esp_wifi_remote` configured — publish a sensor reading to a public test broker and confirm it lands, all without a single line of P4-specific radio code. Then try the MIPI-CSI camera example to see the ISP path light up.
