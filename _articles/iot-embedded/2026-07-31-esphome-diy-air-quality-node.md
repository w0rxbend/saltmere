---
title: "Building a DIY Air-Quality Node with ESPHome: YAML In, Firmware Out"
date: 2026-07-31
track: iot-embedded
summary: "An ESP32 and a Sensirion SEN5x become a networked air-quality sensor from a declarative ESPHome YAML description — no hand-written C, with the native API delivering readings into Home Assistant."
reading_time: 6
tags: [esphome, esp32, home-assistant, air-quality, iot, sensors]
sources:
  - title: "ESPHome 2026.7.0 changelog"
    url: "https://esphome.io/changelog/2026.7.0/"
  - title: "SEN5x Series Environmental sensor component"
    url: "https://esphome.io/components/sensor/sen5x/"
  - title: "Nabu Casa has acquired ESPHome"
    url: "https://www.home-assistant.io/blog/2021/03/18/nabu-casa-has-acquired-esphome/"
  - title: "ESPHome air quality sensor system (SEN6x) — 360customs"
    url: "https://www.360customs.de/en/2026/02/esphome-air-quality-sensor-system-sen6x/"
---

**Gist.** A networked air-quality sensor built directly on ESP-IDF requires hand-written inter-integrated circuit (I²C) transactions, Sensirion cyclic-redundancy-check (CRC) handling, Wi-Fi reconnection, a transport protocol and an over-the-air (OTA) update path before the first reading arrives. ESPHome removes that work by treating a YAML file as the single source of truth and **generating C++ from it at compile time**, so the device runs compiled drivers rather than an interpreted configuration. The cost is expressiveness: anything outside the component model — custom power management, unusual bus timing, tight memory budgets — has to be reached through escape hatches or abandoned for hand-written firmware.

## How ESPHome turns YAML into firmware

ESPHome is a code generator, not an interpreter. Compiling a configuration walks the YAML document, instantiates a C++ object graph for every referenced component, emits `.cpp` and `.h` files, and hands them to the underlying toolchain, which produces a firmware binary. As of **ESPHome 2026.7.0**, ESP-IDF is the **default framework for ESP32 builds**; the configuration below names it explicitly rather than relying on the default.

The load-bearing consequence of code generation is that **the `sensor:` block is not parsed on the device**. The running image contains no YAML text and no configuration interpreter; the update interval, the I²C address and the set of published measurements are all fixed at build time. A configuration change therefore implies a rebuild and a reflash — the device holds no configuration file to edit or reload — and a YAML error surfaces as a build failure on the workstation rather than as a fault on the device.

**Nabu Casa**, which also develops Home Assistant, acquired ESPHome in March 2021. That shared ownership is the context for the tight Home Assistant integration described below, though the acquisition announcement records the ownership change rather than a design rationale for the integration.

## Declaring an I²C bus and a sensor

The Sensirion **SEN5x** family communicates over I²C. The SEN54 and SEN55 report particulate matter (PM), a volatile-organic-compound (VOC) index, relative humidity and temperature; the SEN55 additionally reports a nitrogen-oxides (NOx) index. The bus is declared once and sensor platforms attach to it. Wiring is serial data (SDA) and serial clock (SCL) to two general-purpose input/output (GPIO) pins, plus shared supply and ground. The **default I²C address for the SEN5x is `0x69`**.

```yaml
esphome:
  name: air-quality-node

esp32:
  board: esp32dev
  framework:
    type: esp-idf

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

# Native API — the direct path into Home Assistant
api:
  encryption:
    key: !secret api_encryption_key

# Over-the-air updates after the first USB flash
ota:
  - platform: esphome
    password: !secret ota_password

logger:

i2c:
  sda: GPIO21
  scl: GPIO22
  scan: true

sensor:
  - platform: sen5x
    id: sen55
    address: 0x69
    update_interval: 30s
    pm_1_0:
      name: "PM <1µm"
    pm_2_5:
      name: "PM <2.5µm"
    pm_4_0:
      name: "PM <4µm"
    pm_10_0:
      name: "PM <10µm"
    temperature:
      name: "Temperature"
    humidity:
      name: "Humidity"
    voc:
      name: "VOC Index"
    nox:
      name: "NOx Index"
```

Each key under the platform (`pm_2_5`, `voc`, `nox`, and the rest) does two things at once: it **selects a measurement to read over I²C and declares a published entity**. A key given a `name` becomes a discoverable sensor; a key omitted entirely is neither read nor published. The eight keys above therefore fix both the device's read set and the entity list that appears downstream.

`scan: true` instructs the I²C component to enumerate responding addresses at boot and log them. This is the diagnostic that separates a wiring fault from a configuration fault: if the scan reports no devices, the problem is electrical or pin assignment; if it reports `0x69` but the sensor platform reports failures, the problem lies above the bus.

The sensor **publishes readings before they have stabilised**: values appear as soon as the first measurement cycle completes, while the particulate and gas-index outputs continue to settle after power-up. That matters when an automation threshold is evaluated immediately after a reboot.

## Native API compared with MQTT

Two transports carry readings off the device.

The **native API** is ESPHome's own binary protocol over a persistent connection. Home Assistant discovers the device, holds the connection open, and receives pushed updates with low latency. No message broker is involved. The `api.encryption.key` field enables the encrypted variant of the transport.

**Message Queuing Telemetry Transport (MQTT)** is the alternative: replacing the `api:` block with an `mqtt:` block pointing at a broker makes the node publish to topics instead. MQTT is the appropriate choice when consumers other than Home Assistant need the data — Node-RED, Grafana by way of Telegraf, a bespoke subscriber — or when the node must keep publishing while the Home Assistant server is offline. For a Home-Assistant-only deployment, the native API involves less configuration and lower latency, at the price of a single consumer.

The choice is a build-time one for the same reason the sensor set is: the transport component is compiled in.

## OTA and Home Assistant adoption

The device is flashed over Universal Serial Bus (USB) exactly once. Thereafter the `ota:` component accepts new images over Wi-Fi, so a recompiled configuration is pushed to the running device without physical access. This is what makes the build-time-only configuration model tolerable — a rebuild is required for every change, but a rebuild does not require touching the hardware.

Combined with native-API discovery, the sequence is: flash once over USB; the node appears on the Home Assistant integrations page; adoption publishes all eight declared entities. No entity is wired by hand and no MQTT topic is mapped manually, because the entity list was already fixed by the YAML at compile time.

**Invariant worth stating explicitly:** the YAML file is the only description of the device's behaviour. Nothing on the device diverges from it, because nothing on the device is configurable independently of it. Recovering a device's behaviour means reading its configuration file, not querying the device.

## The trade being made

Implementing the same node directly in ESP-IDF or Arduino means owning the I²C transactions, the Sensirion CRC handling, Wi-Fi reconnection, a discovery and transport protocol, and an OTA mechanism — a substantial body of code before the first reading is published. What that effort buys is complete control: custom power management, unusual bus timing, non-standard protocols, and tight memory budgets are all reachable.

ESPHome exchanges that control for maintained, shared, tested drivers. For a standard sensor on a standard bus reporting to Home Assistant the declarative path is the shorter one; hand-written C becomes necessary when the component model cannot express the requirement, and even then `lambda:` expressions and external components cover part of the gap.

## Pitfalls

- **Readings appear immediately after boot but drift afterwards.** The SEN5x publishes from the first measurement cycle while its outputs are still settling; an automation that fires on a threshold at startup is acting on unstabilised values.
- **The I²C scan finds nothing.** The fault is electrical — SDA/SCL swapped, missing ground, or pins other than the `GPIO21`/`GPIO22` declared in `i2c:`. No amount of sensor-platform configuration compensates.
- **A measurement is missing from Home Assistant.** A key omitted under the `sen5x` platform is not read at all; a key present without a `name` is read but not published as an entity.
- **`nox` is configured on a SEN54.** Only the SEN55 reports the NOx index; the SEN54 does not, so the key describes a measurement the part cannot supply.
- **Editing the YAML does not change device behaviour.** Configuration is compiled in, so a change takes effect only after recompiling and pushing a new image; the device holds no copy of the YAML to reload.
- **The first flash is attempted over OTA.** The `ota:` component lives inside the firmware, so a device with no ESPHome image on it has no OTA endpoint; the initial flash must go over USB.
- **The node stops reporting when Home Assistant is down.** With the native API the device has a single consumer and no broker to buffer for; a deployment that must survive server downtime needs the MQTT transport instead.
