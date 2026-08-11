---
title: "Extend BLE Range into Home Assistant with an ESP32 Bluetooth Proxy"
date: 2026-08-11
track: iot-embedded
summary: "Battery BLE sensors scattered around a building are invisible to a Home Assistant host that can't physically hear them. A cheap ESP32 flashed with ESPHome relays those advertisements — and connectable devices — over Wi-Fi. Here's a minimal working proxy config and how to blanket a house with coverage."
reading_time: 5
tags: [esphome, esp32, bluetooth-proxy, ble, home-assistant, iot]
sources:
  - title: "Bluetooth Proxy — ESPHome"
    url: "https://esphome.io/components/bluetooth_proxy/"
  - title: "ESP32 Bluetooth Low Energy Tracker Hub — ESPHome"
    url: "https://esphome.io/components/esp32_ble_tracker.html"
  - title: "Bluetooth — Home Assistant"
    url: "https://www.home-assistant.io/integrations/bluetooth/"
  - title: "Native API Component — ESPHome"
    url: "https://esphome.io/components/api/"
  - title: "ESPHome Bluetooth Proxy for Home Assistant (ESP32 Guide) — Seeed Studio"
    url: "https://www.seeedstudio.com/blog/2026/03/11/esphome-bluetooth-proxy/"
---

Bluetooth Low Energy was designed for coin-cell devices talking to something a few metres away, not for a house. So the moment you scatter cheap BLE sensors around a building — Xiaomi LYWSD03MMC thermometers reflashed with the ATC firmware, Mi Flora plant probes, a BLE particulate or CO₂ node in the garage — you hit the same wall. Home Assistant runs on one box in one room. Its Bluetooth adapter, if it has one at all, hears the sensors in that room and nothing through two brick walls and a floor. The readings exist; the host just can't listen from where it sits.

A Bluetooth proxy fixes the geometry instead of the radio. You flash a spare ESP32 with ESPHome's `bluetooth_proxy` component, plug it in near the sensors, and it forwards every BLE advertisement it hears over Wi-Fi to Home Assistant using the ESPHome native API. HA treats that remote ESP32 as just another Bluetooth adapter. Put three or four proxies around the house and you have building-wide BLE coverage for the price of a few dev boards.

## Passive vs active proxy modes

There are two things a proxy can do, and they matter for what devices you can reach.

**Passive** proxying relays the advertising packets that BLE sensors broadcast unsolicited. Most battery sensors — ATC thermometers, plant sensors, air-quality beacons — publish their readings right in the advertisement, so passive mode is enough to see temperature, humidity, and battery with zero connection overhead and no extra drain on the sensor's cell.

**Active** proxying adds the ability to open a real GATT connection through the proxy — the same connectable devices we built by hand in the [ESP32 BLE GATT server article]({{ site.baseurl }}/articles/iot-embedded/esp32-ble-gatt-nimble/). That's what you need for locks, some Switchbot devices, and any sensor whose data only comes over a connection rather than an advertisement. Active mode also enables *active scanning*, where the proxy sends a scan request to pull the scan-response payload (device names and extra data) that some sensors only reveal when asked.

Active connections are relayed to Home Assistant only when the native API is **encrypted** — an unencrypted API will still pass advertisements but refuses to broker connections. So set an encryption key regardless.

## A minimal working proxy

This is a complete, buildable config for a plain ESP32. It enables active proxying, sets an encryption key, and keeps everything else deliberately light.

```yaml
esphome:
  name: ble-proxy-garage

esp32:
  board: esp32dev
  framework:
    type: esp-idf

# Native API — the transport to Home Assistant.
# The encryption key is required for active connections.
api:
  encryption:
    key: "REPLACE_WITH_A_BASE64_KEY"

ota:
  - platform: esphome

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

logger:
  # Trim BLE log spam once it works; verbose logging costs heap.
  level: INFO

# The scanner. Passive scanning is easier on heat and CPU.
esp32_ble_tracker:
  scan_parameters:
    interval: 320ms
    window: 30ms
    active: false

bluetooth_proxy:
  active: true
```

Generate the encryption key from **Settings → Devices → ESPHome → new device** in Home Assistant, or with any base64-encoded 32-byte value. Note the split personality of the two `active` flags: `esp32_ble_tracker`'s `active` controls scan requests (leave it `false` to run cooler for advertisement-only sensors), while `bluetooth_proxy`'s `active` controls GATT connections. You can run passive scanning and active connections together, which is a sensible default.

## Flashing and adding coverage

For the first flash you need USB — the initial build repartitions flash to make room for the BLE stack, so it can't be done purely over the air. Two easy paths:

| Method | When to use |
| --- | --- |
| [web.esphome.io](https://web.esphome.io) | No install; flash straight from Chrome/Edge over USB. Good for the first device. |
| ESPHome dashboard (HA add-on) | Manages secrets, OTA, and rebuilds for a fleet of proxies. |

After the first cable flash, every later change goes over the air via the `ota:` block. Home Assistant auto-discovers the device; confirm the encryption key and the Bluetooth integration immediately lists the proxy as a remote adapter. Drop a second and third board in the far corners of the house and HA merges their coverage automatically, preferring whichever adapter hears a given sensor best.

## Keep it lean

BLE proxying is genuinely resource-hungry. The ESP32 Bluetooth stack eats a large slice of RAM, and relaying scan traffic while juggling connection slots keeps the CPU busy. The ESPHome docs are blunt about it: pile on too many components and the device will crash. So a proxy should be *just* a proxy. Resist the urge to bolt a display, voice assistant, or a stack of I²C sensors onto the same board — if you want an environmental node too, build a dedicated one following the [ESPHome DIY air-quality node]({{ site.baseurl }}/articles/iot-embedded/esphome-diy-air-quality-node/) and let this ESP32 do one job well.

Classic ESP32 and the ESP32-S3 are the comfortable choices, with two cores and enough headroom for several connection slots. Single-core parts like the ESP32-C3 work fine for passive advertisement relaying but have less slack, so keep the config minimal and expect fewer simultaneous active connections. If you plan to reach connectable devices, tune `bluetooth_proxy`'s `connection_slots` (1–9) to match how many locks or Switchbots live in that zone — each slot costs heap.

**Try next:** flash a second proxy at the opposite end of the building, then watch **Settings → System → Repairs → Bluetooth** in Home Assistant to see which adapter each sensor binds to as you move a thermometer between rooms.
