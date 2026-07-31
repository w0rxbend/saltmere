---
title: "Build a DIY Air-Quality Node with ESPHome: YAML In, Firmware Out"
date: 2026-07-31
track: iot-embedded
summary: "Turn an ESP32 and a Sensirion SEN5x into a networked air-quality sensor using declarative ESPHome YAML — no hand-written C, with the native API dropping it straight into Home Assistant."
reading_time: 5
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

You can have a networked air-quality sensor reporting into your dashboard in about fifteen minutes, and you will not write a single line of C. ESPHome takes a YAML description of your board, network, and peripherals and compiles it into a complete firmware image for the ESP32. You describe *what* you want; it generates the C++, links the drivers, and flashes the chip.

## How ESPHome turns YAML into firmware

ESPHome is a code generator, not an interpreter. When you compile a config, it walks your YAML, instantiates a C++ object graph for every component you referenced, emits `.cpp`/`.h` files, and hands them to the underlying toolchain to build a real firmware binary. As of **ESPHome 2026.7.0** (July 2026), ESP32 builds use the **ESP-IDF native toolchain by default**, having moved off PlatformIO. The important consequence: your `sensor:` block isn't parsed at runtime — it becomes compiled-in driver code, so the running device carries no YAML and no interpreter overhead.

ESPHome is now owned by the **Open Home Foundation** and developed by **Nabu Casa** (which acquired the project back in 2021), the same organization behind Home Assistant. That shared lineage is why integration is so seamless.

## Defining an I²C bus and a sensor

Air-quality parts like the Sensirion **SEN5x** family (SEN54/SEN55 measure PM, VOC, humidity, temperature; the SEN55 adds NOx) speak I²C. In ESPHome you declare the bus once, then attach a sensor platform to it. Wire the sensor's SDA/SCL to two GPIOs, share 3.3V/5V and ground, and the config below does the rest. The default I²C address for the SEN5x is `0x69`.

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

# Native API — the fast path into Home Assistant
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

Each key under the platform (`pm_2_5`, `voc`, `nox`, …) both selects a measurement to read over I²C and declares a published entity. Give it a `name` and it becomes a discoverable sensor. Note the sensor needs roughly a minute of warm-up before readings stabilize.

## Native API vs. MQTT

Two ways to get readings off the device. The **native API** is ESPHome's own binary, persistent-connection protocol. Home Assistant discovers the device, holds an open connection, and receives push updates with low latency — no broker to run. The alternative is **MQTT**: swap the `api:` block for an `mqtt:` block pointing at your broker, and the node publishes to topics instead. Choose MQTT when you need the data in something *other* than Home Assistant (Node-RED, Grafana via Telegraf, a custom subscriber), or when you want the device to keep working with the HA server offline. For a pure Home Assistant setup, the native API is faster to configure and lower-latency.

## OTA and landing in Home Assistant

You flash over USB exactly once. After that, the `ota:` component lets every future config change go out over Wi-Fi — recompile, and ESPHome pushes the new image to the running device. Combined with the native API's auto-discovery, the workflow is: flash once, then a node appears in Home Assistant's integrations page. Adopt it, and all eight sensor entities show up automatically, ready for dashboards and automations. No entity wiring, no manual MQTT topic mapping.

## The trade you're making

Hand-writing this in ESP-IDF or Arduino means owning the I²C transactions, the Sensirion CRC handling, the Wi-Fi reconnect logic, a discovery/transport protocol, and your own OTA mechanism — easily a few hundred lines before the first reading. What you buy with that effort is total control: custom power management, exotic bus timing, non-standard protocols, tight memory budgets. ESPHome trades that control for speed and correctness — the drivers are maintained, tested, and shared. For a standard sensor on a standard bus talking to Home Assistant, the declarative path wins decisively; you drop to custom C only when you hit something the component model can't express (and even then, `lambda:` and external components give you an escape hatch).

**Try next:** Wire a SEN5x (or an SCD4x for CO₂) to GPIO21/22, flash the config above, then add a Home Assistant automation that turns on a fan or sends a notification when `PM <2.5µm` crosses 35 µg/m³.
