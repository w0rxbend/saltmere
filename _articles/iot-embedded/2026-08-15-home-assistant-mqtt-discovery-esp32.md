---
title: "Home Assistant MQTT Discovery From a Bare ESP32: One Retained JSON Config Creates the Entity"
date: 2026-08-15
summary: "ESPHome and Zigbee2MQTT obtain auto-discovery from the integration; hand-written ESP-IDF or Arduino firmware can speak the same convention. The discovery topic layout, the device-based single-message form, last-will availability, and the role of unique_id plus a device block in grouping five sensors under one device."
track: iot-embedded
reading_time: 6
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

**Gist.** A node running custom firmware publishes readings to a broker, but Home Assistant has no way to know what those readings mean, so every entity must otherwise be declared by hand in YAML. MQTT Discovery replaces that with a convention: the firmware publishes one retained JavaScript Object Notation (JSON) configuration message to a well-known topic, and Home Assistant creates the entity, its unit, device class and device page from it. The cost is that the broker now holds authoritative configuration state — a wiped retained message removes the entities, and a duplicated identifier silently drops one of them.

The [ESPHome node](/articles/iot-embedded/2026-07-31-esphome-diy-air-quality-node/) appears automatically because ESPHome speaks this convention, not because of anything internal to it. Broker mechanics — the esp-mqtt client, quality of service (QoS), session handling — are covered in the [MQTT 5 article](/articles/iot-embedded/2026-08-10-esp32-mqtt5-esp-mqtt/); what follows concerns only the payloads.

## The topic convention

Per-entity discovery uses:

```
<discovery_prefix>/<component>/[<node_id>/]<object_id>/config
```

`discovery_prefix` defaults to `homeassistant`, `component` is the entity platform (`sensor`, `binary_sensor`, `switch`, …), and `node_id`/`object_id` are chosen by the publisher from the character set `[a-zA-Z0-9_-]`. A carbon dioxide (CO2) reading therefore lives at `homeassistant/sensor/aqnode01/co2/config`. The payload is JSON describing the entity: where its state is published, its units, its device class.

Two retention rules define the lifecycle. The configuration message is published **retained**, so a restarted Home Assistant receives it again from the broker on resubscription rather than waiting for the node to reboot. Publishing an **empty retained payload to the same topic deletes the entity** — the topic that creates the entity is the one that clears it.

Since Home Assistant **2024.11** a second form exists for multi-sensor nodes: **device-based discovery**, a single message at

```
homeassistant/device/<object_id>/config
```

describing the whole node. It carries a `dev` (device) block, an `o` (origin) block naming the firmware, and a `cmps` map with one entry per component, each entry carrying a `p` (platform) key. One publish replaces one message per entity, and the device-to-entity relationship is stated directly rather than reconstructed by matching `device.identifiers` across separate messages.

## The payload for a CO2 node

A device-based payload for an SCD41 node (the sensor itself belongs to the [true-CO2 article](/articles/iot-embedded/2026-07-31-scd41-true-co2-i2c-esp32/)):

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

The abbreviated keys (`avty_t` for `availability_topic`, `stat_t` for `state_topic`, `uniq_id` for `unique_id`, `dev_cla` for `device_class`) are documented aliases, not shorthand invented here, and reduce payload size — which matters on a microcontroller where the client buffer is a fixed allocation.

Three fields are load-bearing:

- **`unique_id`** — without it the entity has no entry in the entity registry, and therefore cannot be renamed or assigned to an area; it exists only as long as its state does. With it, together with the `dev` block, every component collapses onto one device page. It must be derived from something **stable across reboots**, such as the media access control (MAC) address (`aqnode-%02x%02x%02x`).
- **`expire_after`** — seconds after the last state message before the entity is marked `unavailable`. The **default is 0, meaning never expire**, so an unset value leaves a crashed node displaying its final reading indefinitely. A value comfortably larger than the publish interval makes the crash visible without flagging a single missed reading.
- **`avty_t`** — the availability topic, described next.

## Availability, last will, and the birth message

Availability rests on a broker-side mechanism rather than on the firmware. The client registers a **Last Will and Testament (LWT)** at connect time: topic `aqnode01/status`, payload `offline`, retained. Immediately after the connection succeeds the firmware publishes `online`, retained, to the same topic. The invariant is that **the topic is written either by the node when it is alive or by the broker when the session ends**, so it stays truthful across a power cut, a crash, or a Wi-Fi drop where the firmware never executes a shutdown path. The payloads Home Assistant expects by default are the literal strings `online` and `offline` (`payload_available` / `payload_not_available`).

The reverse direction also has a hook: on its own restart Home Assistant publishes a **birth message** to `homeassistant/status`. Retained discovery configurations already cover a plain restart, so subscribing to the birth topic and republishing discovery on `online` matters in the case where the broker's retained store has been cleared — a broker reinstall or a purge — and the retained configuration no longer exists to replay.

Retention then divides cleanly: discovery configurations retained, availability retained, state retained only where a stale value shown at Home Assistant startup is acceptable. With `expire_after` set, retained state carries a bounded staleness rather than an unbounded one.

## Arduino: publishing discovery and state

With `PubSubClient` the packet buffer defaults to 256 bytes, and a publish exceeding it is dropped locally: `publish()` returns `false` and nothing leaves the node. The device discovery payload above is larger than that, so unless the return value is checked the entity never appears and nothing reports why. `setBufferSize()` must be called before connecting.

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
  mqtt.publish("aqnode01/co2", buf, true);       // retained state; caller sets the cadence
}
```

That is the complete integration: no YAML on the Home Assistant side, and the node registers as one device with a CO2 entity carrying the `ppm` unit and long-term statistics, the latter following from `state_class: measurement`. Where hand-assembled JSON is unwelcome, the `HAMqttDevice` Arduino library constructs per-entity payloads; on ESP-IDF, `cJSON` is already present in the tree.

A useful check: power-cycle the node while the device page is open. The LWT should mark it unavailable once the broker declares the session dead on keepalive expiry, and `expire_after` covers the distinct case where the network connection survives but the sensor task stops producing readings.

## Pitfalls

- Discovery configuration published without the retain flag: entities exist until Home Assistant restarts, then disappear, because there is nothing in the broker to replay.
- Two entities sharing a `unique_id`: Home Assistant raises an exception and ignores the second entity; the first appears normally, which makes the omission look like a publish failure.
- No `device.identifiers` (`ids`) in the `dev` block, or differing identifiers across per-entity messages: the entities are created but scatter as unrelated devices instead of grouping.
- `PubSubClient` buffer left at its 256-byte default: publishes of larger discovery payloads are dropped locally; `publish()` returns `false`, and a sketch that ignores the return value sees no entity and no diagnostic.
- `expire_after` unset: a crashed node keeps its last reading on the dashboard indefinitely, since the default of 0 disables expiry.
- LWT registered but no retained `online` published after connect: the retained `offline` from the previous disconnect remains the last value on the availability topic, and the entity stays unavailable while the node is running.
- `unique_id` derived from a value that changes across reboots: each restart creates a new registry entry, and earlier renames and area assignments are stranded on orphaned entities.
