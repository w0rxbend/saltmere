---
title: "Extending BLE Range into Home Assistant with an ESP32 Bluetooth Proxy"
date: 2026-08-11
track: iot-embedded
summary: "Battery BLE sensors scattered around a building are invisible to a Home Assistant host that cannot physically hear them. An ESP32 flashed with ESPHome relays those advertisements — and connectable devices — over Wi-Fi. A minimal working proxy configuration, and how coverage is extended across a house."
reading_time: 6
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

**Gist.** Bluetooth Low Energy (BLE) is a short-range radio protocol, so a Home Assistant host in one room cannot hear coin-cell sensors two walls away; the readings are broadcast but never received. An ESP32 running ESPHome's `bluetooth_proxy` component sits near the sensors and forwards the BLE traffic it hears to Home Assistant over Wi-Fi via the ESPHome native application programming interface (API), where it is presented as an additional Bluetooth adapter. The cost is a dedicated microcontroller per zone: the ESP32 Bluetooth stack consumes a large share of the chip's RAM, and the ESPHome documentation recommends against adding other components to the same device.

## The problem is geometry, not sensitivity

Devices such as Xiaomi LYWSD03MMC thermometers reflashed with the ATC firmware, Mi Flora plant probes, or a BLE particulate or carbon-dioxide node in a garage transmit continuously. The failure is that **no receiver is within range**. Increasing the host's transmit power changes nothing, because the limiting direction is inbound. A proxy relocates the receiver rather than improving it: the ESP32 performs the radio work locally and the Wi-Fi link — which already spans the building — carries the result.

The relayed unit is the raw advertisement, not a decoded sensor value. Decoding remains in Home Assistant, so a proxy needs no knowledge of any particular sensor's payload format and needs no reconfiguration when a new sensor type is introduced.

## Passive and active proxying

Two distinct capabilities are involved, and they determine which devices are reachable.

**Passive** proxying relays the advertising packets that BLE peripherals broadcast unsolicited. Sensors that publish their readings inside the advertisement — ATC thermometers, plant sensors, air-quality beacons — are fully served by passive mode. **No connection is opened, so the sensor's cell incurs no additional drain.**

**Active** proxying additionally allows a Generic Attribute Profile (GATT) connection to be opened *through* the proxy, reaching the same class of connectable peripheral built by hand in the [ESP32 BLE GATT server article]({{ site.baseurl }}/articles/iot-embedded/2026-07-30-esp32-ble-gatt-nimble/). This is required for locks, some Switchbot devices, and any sensor whose data is exposed only over a connection. A separate setting on the scanner enables **active scanning**, in which the proxy transmits a scan request to elicit the scan-response payload — device names and additional data that some peripherals reveal only when asked. The two settings are configured independently; the next section separates them.

One constraint is absolute: **active connections are relayed only when the native API is encrypted.** An unencrypted API continues to pass advertisements but refuses to broker connections. The practical consequence is a silent partial failure — sensors appear, locks do not — so an encryption key belongs in every proxy configuration regardless of present intent.

## A minimal working configuration

The following is a complete, buildable configuration for a plain ESP32. It enables active proxying, sets an encryption key, and omits everything else.

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
  # Verbose BLE logging costs heap; INFO is the steady-state level.
  level: INFO

# The scanner. Passive scanning transmits no scan requests.
esp32_ble_tracker:
  scan_parameters:
    interval: 320ms
    window: 30ms
    active: false

bluetooth_proxy:
  active: true
```

The encryption key is generated from **Settings → Devices → ESPHome → new device** in Home Assistant, or supplied as any base64-encoded 32-byte value.

The two `active` keys are unrelated despite the shared name, and conflating them is the most common configuration error. **`esp32_ble_tracker.active` controls scan requests**; leaving it `false` avoids transmitting a scan request for every advertisement seen, which is sufficient where only advertisement-based sensors are in range. **`bluetooth_proxy.active` controls GATT connections.** The combination in the snippet — passive scanning with active connections — is coherent: connections are brokered on demand without the scanner soliciting scan responses.

The `scan_parameters` pair defines a duty cycle: a **30 ms receive window inside every 320 ms interval**, so the radio listens for roughly one-tenth of the time. A peripheral whose advertising interval is long relative to that window is heard less often, which manifests as delayed rather than absent updates.

## Flashing and extending coverage

The first flash requires universal serial bus (USB) cabling: a factory-fresh board carries no ESPHome firmware, so there is nothing yet listening for an over-the-air update.

| Method | When to use |
| --- | --- |
| [web.esphome.io](https://web.esphome.io) | No installation; flashes over USB from Chrome or Edge. Suitable for the first device. |
| ESPHome dashboard (Home Assistant add-on) | Manages secrets, over-the-air (OTA) updates and rebuilds across a fleet of proxies. |

Every subsequent change is delivered over the air through the `ota:` block. Home Assistant auto-discovers the device; once the encryption key is confirmed, the Bluetooth integration lists the proxy as a remote adapter. Additional boards placed at the far corners of a building have their coverage merged automatically, with Home Assistant preferring whichever adapter hears a given sensor best.

## Resource budget

BLE proxying is resource-hungry. The ESP32 Bluetooth stack occupies a large portion of available RAM, and relaying scan traffic while maintaining connection slots keeps the processor busy. **A proxy should therefore run no other workload.** A display, a voice assistant, or a set of Inter-Integrated Circuit (I²C) sensors on the same board competes for the same heap; an environmental node belongs on separate hardware, as in the [ESPHome DIY air-quality node]({{ site.baseurl }}/articles/iot-embedded/2026-07-31-esphome-diy-air-quality-node/).

The classic ESP32 and the ESP32-S3 are the comfortable choices: two cores and sufficient headroom for several connection slots. Single-core parts such as the ESP32-C3 handle passive advertisement relaying but carry less slack, so configurations there should stay minimal and fewer simultaneous active connections should be expected. Where connectable devices are in scope, `bluetooth_proxy`'s **`connection_slots` (range 1–9)** is set to the number of locks or Switchbots in that zone; **each slot costs heap**, so over-provisioning trades stability for capacity that is never used.

## Pitfalls

- **Locks and Switchbots never appear while thermometers do.** The native API is unencrypted, so advertisements pass but connection brokering is refused.
- **`active: true` set on `esp32_ble_tracker` instead of `bluetooth_proxy`.** Scan requests are transmitted — raising radio activity and heat — while GATT connections remain unavailable, which is the opposite of the intended effect.
- **The device reboots or crashes after adding components.** The BLE stack's RAM footprint leaves little margin, which is why ESPHome recommends running no other components on a proxy device.
- **An initial flash attempted over the air fails.** A board with no ESPHome firmware on it has no OTA endpoint to receive the image; the first flash is over USB.
- **`connection_slots` raised speculatively.** Each slot consumes heap whether or not a device occupies it, reducing the margin available to the scanner.
- **Verbose logging left enabled after commissioning.** Log output consumes heap on a device that has little to spare.
