---
title: "Home Assistant MQTT Discovery From a Bare ESP32: One Retained JSON and Your Sensor Just Appears"
date: 2026-08-15
track: iot-embedded
summary: "ESPHome and Zigbee2MQTT get auto-discovery for free — your hand-rolled ESP-IDF or Arduino firmware can too. The discovery topic convention, the newer single-message device-based discovery, LWT availability, and why unique_id plus a device block is what makes five sensors show up as one device."
reading_time: 5
tags: [home-assistant, mqtt, discovery, esp32, arduino, co2]
sources:
  - title: "MQTT integration — MQTT Discovery (Home Assistant docs)"
    url: "https://www.home-assistant.io/integrations/mqtt/"
  - title: "MQTT Sensor — configuration variables incl. expire_after (Home Assistant docs)"
    url: "https://www.home-assistant.io/integrations/sensor.mqtt/"
  - title: "Home Assistant 2024.11 release notes — MQTT device-based auto discovery"
    url: "https://www.home-assistant.io/blog/2024/11/06/release-202411/"
  - title: "Home Assistant MQTT Auto-Discovery — Mike Polinowski"
    url: "https://mpolinowski.github.io/docs/Automation_and_Robotics/Home_Automation/2022-07-10-home-assistant-mqtt-autodiscovery-part-i/2022-07-10/"
  - title: "plapointe6/HAMqttDevice — Arduino helper for HA discovery payloads"
    url: "https://github.com/plapointe6/HAMqttDevice"
---

The [ESPHome node](/articles/iot-embedded/2026-07-31-esphome-diy-air-quality-node/) appears in Home Assistant by magic; the node running your own firmware sits invisible until someone hand-writes YAML for every entity. The magic is not ESPHome — it's **MQTT Discovery**, a plain convention any firmware can speak: publish one retained JSON config to the right topic and Home Assistant creates the entity, icon, units and device page for you. The broker mechanics (esp-mqtt client, QoS, sessions) are covered in the [MQTT 5 article](/articles/iot-embedded/2026-08-10-esp32-mqtt5-esp-mqtt/); this is purely about what to publish.

## The topic convention

Per-entity discovery uses:

```
<discovery_prefix>/<component>/[<node_id>/]<object_id>/config
```

`discovery_prefix` defaults to `homeassistant`, `component` is the entity platform (`sensor`, `binary_sensor`, `switch`, …), and `node_id`/`object_id` are yours to choose from `[a-zA-Z0-9_-]`. So a CO2 reading lives at `homeassistant/sensor/aqnode01/co2/config`. The payload is JSON describing the entity: where its state is published, units, device class. Publish it **retained** so a restarted Home Assistant replays it from the broker; publish an empty retained payload to the same topic to delete the entity.

Since Home Assistant **2024.11** there is a better shape for multi-sensor nodes: **device-based discovery**. One message at

```
homeassistant/device/<object_id>/config
```

describes the whole node — a `dev` (device) block, an `o` (origin) block naming your firmware, and a `cmps` map with one entry per component, each carrying a `p` (platform) key. One publish instead of five, and the device/entity relationship is explicit instead of reassembled from matching `device.identifiers`.

## The payload for a CO2 node

Here is a device-based payload for an SCD41 node (the sensor itself is the [true-CO2 article](/articles/iot-embedded/2026-07-31-scd41-true-co2-i2c-esp32/)'s territory):

```json
{
  "dev":  { "ids": ["aqnode01"], "name": "Air Quality Node 01",
            "mf": "DIY", "mdl": "ESP32 + SCD41", "sw": "1.4.0" },
  "o":    { "name": "aqnode-firmware", "sw": "1.4.0",
            "url": "https://github.com/you/aqnode" },
  "avty_t": "aqnode01/status",
  "cmps": {
    "co2": {
      "p": "sensor", "uniq_id": "aqnode01_co2",
      "name": "CO2", "dev_cla": "carbon_dioxide",
      "stat_t": "aqnode01/co2", "unit_of_meas": "ppm",
      "stat_cla": "measurement", "expire_after": 300
    },
    "temp": {
      "p": "sensor", "uniq_id": "aqnode01_temp",
      "name": "Temperature", "dev_cla": "temperature",
      "stat_t": "aqnode01/temp", "unit_of_meas": "°C",
      "stat_cla": "measurement", "expire_after": 300
    }
  }
}
```

The abbreviations (`avty_t` = `availability_topic`, `stat_t` = `state_topic`, `uniq_id` = `unique_id`, `dev_cla` = `device_class`) are official and worth using on a microcontroller — payloads shrink by half.

Three fields do the heavy lifting:

- **`unique_id`** — without it the entity is ephemeral: no entity registry entry, no renaming, no area assignment. With it, plus the `dev` block, all components group under one device page. Derive it from something stable like the MAC (`aqnode-%02x%02x%02x`), never from anything that changes across reboots.
- **`expire_after`** — seconds until a stale state flips the entity to `unavailable` (default 0 = never). Set it to 2–3× your publish interval; a crashed node then *shows* as dead instead of freezing its last reading on the dashboard forever.
- **`avty_t`** — availability, next.

## Availability, LWT, and the birth message

Register a Last Will when connecting: topic `aqnode01/status`, payload `offline`, retained. After connecting, publish `online` (retained) to the same topic. The broker publishes the will for you when the node drops off — so availability is truthful even when the firmware never gets a chance to say goodbye. The defaults Home Assistant expects are literally `online`/`offline` (`payload_available`/`payload_not_available`).

The reverse direction matters too: when Home Assistant itself restarts it publishes a **birth message** to `homeassistant/status`. Retained discovery configs cover the restart case, but subscribing to the birth topic and re-publishing discovery on `online` is cheap insurance against a wiped broker.

Retain rules of thumb: discovery configs **retained**, availability **retained**, state — retained only if a stale-looking value at HA startup is acceptable; with `expire_after` set, retained state is safe and gives instant dashboards.

## Arduino: publish discovery + state

With `PubSubClient` the one real gotcha is the default 256-byte packet buffer — a device discovery payload will be silently dropped. Call `setBufferSize()` first.

```cpp
#include <WiFi.h>
#include <PubSubClient.h>

WiFiClient net;
PubSubClient mqtt(net);

const char* AVTY = "aqnode01/status";

void connectMqtt() {
  mqtt.setServer("192.168.1.10", 1883);
  mqtt.setBufferSize(1024);                      // discovery JSON > 256 B default
  while (!mqtt.connect("aqnode01", "user", "pass",
                       AVTY, 1, true, "offline"))  // LWT: retained "offline"
    delay(1000);
  mqtt.publish(AVTY, "online", true);            // retained birth

  const char* disco =
    "{\"dev\":{\"ids\":[\"aqnode01\"],\"name\":\"Air Quality Node 01\","
    "\"mdl\":\"ESP32 + SCD41\",\"mf\":\"DIY\"},"
    "\"o\":{\"name\":\"aqnode-firmware\",\"sw\":\"1.4.0\"},"
    "\"avty_t\":\"aqnode01/status\","
    "\"cmps\":{\"co2\":{\"p\":\"sensor\",\"uniq_id\":\"aqnode01_co2\","
    "\"name\":\"CO2\",\"dev_cla\":\"carbon_dioxide\",\"stat_t\":\"aqnode01/co2\","
    "\"unit_of_meas\":\"ppm\",\"stat_cla\":\"measurement\",\"expire_after\":300}}}";
  mqtt.publish("homeassistant/device/aqnode01/config", disco, true);  // retained
}

void publishCo2(uint16_t ppm) {
  char buf[8];
  snprintf(buf, sizeof(buf), "%u", ppm);
  mqtt.publish("aqnode01/co2", buf, true);       // state, every 60 s
}
```

That's the entire integration: no YAML on the Home Assistant side, and the node appears as one device with a proper CO2 entity, correct icon, `ppm` unit, and long-term statistics (courtesy of `state_class: measurement`). If you'd rather not hand-assemble JSON, the small `HAMqttDevice` Arduino library builds per-entity payloads; on ESP-IDF, `cJSON` is already in the tree.

The failure modes are all predictable: forgot `retained` on config → entities vanish when HA restarts; duplicated `unique_id` → HA raises an exception and ignores the second entity; no `device.identifiers` → entities scatter instead of grouping; buffer too small → nothing appears and nothing errors.

**Try next:** power-cycle the node while watching the device page — LWT should flip it to unavailable within the broker's keepalive, and `expire_after` should catch the case where Wi-Fi stays up but your sensor task wedges.
