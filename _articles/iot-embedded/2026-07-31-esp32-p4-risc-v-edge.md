---
title: "The ESP32-P4: a RISC-V Application Processor Without a Radio"
date: 2026-07-31
track: iot-embedded
summary: "Espressif's ESP32-P4 is a dual-core 400 MHz RISC-V SoC with a vector extension, MIPI display and camera interfaces, USB 2.0 High-Speed, and an H.264 encoder — but no radio. What the silicon offers, and how esp_hosted and esp_wifi_remote restore Wi-Fi and MQTT by pairing it with an ESP32-C6."
reading_time: 6
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

**Gist.** The ESP32 family has, until now, equated "system-on-chip (SoC)" with "microcontroller that carries its own radio"; the ESP32-P4 is an application processor and carries no radio at all. The mechanism that restores connectivity is a two-chip split: a wireless companion SoC — an ESP32-C6 or ESP32-C5 — runs the radio and the Wi-Fi driver, while `esp_hosted` transports the driver's calls across a Secure Digital Input Output (SDIO), Serial Peripheral Interface (SPI) or Universal Asynchronous Receiver/Transmitter (UART) link and `esp_wifi_remote` re-exports the ordinary `esp_wifi_*` API on the application side. The cost is a second processor to power, provision and keep in firmware lockstep, plus a network stack whose data path now crosses a physical bus.

## What is on the die

The ESP32-P4 was announced on 5 January 2023 and ships on development boards today. Its compute is a **dual-core 32-bit RISC-V high-performance core running at up to 400 MHz**, implementing RV32IMAFC with a single-precision floating-point unit (FPU), alongside a **third low-power RISC-V core at up to 40 MHz** for always-on duties. For digital signal processing (DSP) and inference work the high-performance cores carry a **custom vector (single instruction, multiple data, SIMD) instruction extension**.

The peripheral set is where the P4 departs from the ESP32-S3 rather than merely outrunning it:

- **Mobile Industry Processor Interface Display Serial Interface (MIPI-DSI) with 2 data lanes** and **MIPI-CSI (Camera Serial Interface) with 2 data lanes and an integrated image signal processor (ISP)**, both rated up to 1080p. This is a dedicated serial display and camera path rather than a parallel LCD bus driven from general-purpose I/O.
- **USB 2.0 High-Speed On-The-Go (OTG)** at 480 Mbit/s.
- An **H.264 hardware encoder**, so compressed video can leave the chip in place of raw frames.
- **768 KB of high-performance L2 static RAM (SRAM)** plus 32 KB of low-power SRAM, and in-package **pseudo-static RAM (PSRAM) options up to 32 MB**. The Waveshare P4-Nano board ships a 32 MB part.

Those interfaces define the class of device the part addresses: a smart display, a camera doorbell, or an instrument with a live graphing user interface and on-device anomaly detection — work that previously implied a Linux single-board computer next to the microcontroller.

## The absent radio and the hosted architecture

The P4 has **no native Wi-Fi and no native Bluetooth**. Espressif's documented architecture pairs it with a wireless companion — the **ESP32-C6 (Wi-Fi 6 and Bluetooth Low Energy 5)** or the ESP32-C5 — over SDIO, SPI or UART. The reference hardware, the ESP32-P4-Function-EV-Board and the Waveshare P4-Nano, wires a C6 to the P4 over SDIO and pre-flashes the C6 with hosted-slave firmware.

Two software layers make the split usable. **`esp_hosted`** is the transport driver, present on both chips: the slave side runs the actual Wi-Fi driver and radio, the host side packages requests and carries network frames across the bus. **`esp_wifi_remote`** sits above it and re-exports the familiar `esp_wifi_*` and `esp-netif` surface on the P4, forwarding each call to the companion. The **property that carries the split is API compatibility**: application code that compiles against the ordinary ESP-IDF Wi-Fi and network interface API compiles unchanged, and the bus crossing is not visible in the source.

A P4 project therefore configures the companion rather than the radio:

```bash
idf.py set-target esp32p4
idf.py add-dependency "espressif/esp_wifi_remote"
idf.py add-dependency "espressif/esp_hosted"
idf.py menuconfig       # select the ESP32-C6 slave and the SDIO transport
idf.py build flash monitor
```

With that configuration in place the stock ESP-IDF protocol examples build as they stand: `examples/protocols/mqtt` pointed at a broker connects through the C6 as though the radio sat on the P4. Target support for the P4 arrived in the mainline ESP-IDF v5.3 line.

The consequence for board design is that an "ESP32 project" becomes a **two-chip project** — an application core plus a wireless coprocessor — with a bus, a pin budget, two flash images and two power domains to account for.

### Where the boundary shows

The abstraction is source-level, not behavioural. Every frame the P4 sends or receives traverses the host-to-slave link, so the SDIO, SPI or UART channel is the **throughput and latency ceiling of the whole network path**, independent of what the radio negotiates over the air. UART in particular is a far narrower channel than SDIO; the reference boards use SDIO. Likewise, the Wi-Fi state machine — scan, association, disconnection, reconnection — runs on the companion, and the P4 observes it only through events relayed across the transport. When the link stalls, the application sees a Wi-Fi API that stops answering rather than a bus that stopped moving bytes.

## Pitfalls

- **Selecting `esp32p4` and calling `esp_wifi_init()` without the hosted components fails to link or fails at runtime**, because the P4 has no radio driver of its own; the symbols come from `esp_wifi_remote`, which must be added as a dependency and configured for a specific slave and transport.
- **Host and slave firmware are a matched pair.** Flashing a new `esp_hosted` host build onto the P4 while leaving stale slave firmware on the C6 produces a link that enumerates but then misbehaves on the protocol layer, not an obvious version error.
- **Wi-Fi throughput measured on the P4 measures the transport, not the air interface.** A result well below the negotiated Wi-Fi rate is explained by the SDIO, SPI or UART channel and by the transport chosen in `menuconfig`, not by radio conditions.
- **A UART transport chosen for pin economy silently caps the network.** The pin saving is real, and so is the bandwidth ceiling it imposes on every socket on the device.
- **The low-power core and the companion are separate power problems.** Keeping the 40 MHz low-power RISC-V core awake for always-on duties does nothing to reduce the companion's draw; the C6 has its own power state, and an always-associated radio is an always-powered second chip.
- **MIPI-DSI and MIPI-CSI are two-lane interfaces.** A panel or sensor requiring more than 2 data lanes does not attach to the P4's interface regardless of resolution headroom.
