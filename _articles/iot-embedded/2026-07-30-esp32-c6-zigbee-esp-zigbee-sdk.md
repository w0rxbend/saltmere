---
title: "Zigbee on the ESP32-C6/H2 with esp-zigbee-sdk: a temperature sensor from scratch"
date: 2026-07-30
track: iot-embedded
summary: "How to build a real Zigbee end device on the ESP32-C6 or ESP32-H2 — the 802.15.4 radio, device roles, the endpoint/cluster/attribute model, and reporting temperature to Home Assistant's ZHA."
reading_time: 5
tags: [zigbee, esp32-c6, esp32-h2, esp-idf, esp-zigbee-sdk, home-assistant, 802.15.4]
sources:
  - title: "espressif/esp-zigbee-sdk (GitHub)"
    url: "https://github.com/espressif/esp-zigbee-sdk"
  - title: "ESP Zigbee SDK Programming Guide (ESP32-C6)"
    url: "https://docs.espressif.com/projects/esp-zigbee-sdk/en/latest/esp32c6/index.html"
  - title: "ESP Zigbee SDK — Attribute API reference"
    url: "https://docs.espressif.com/projects/esp-zigbee-sdk/en/latest/esp32/api-reference/esp_zigbee_attribute.html"
  - title: "ESP Zigbee SDK — Zigbee ZCL General Report user guide"
    url: "https://docs.espressif.com/projects/esp-zigbee-sdk/en/latest/esp32c6/user-guide/zcl_general_report.html"
  - title: "Home Assistant — Zigbee Home Automation (ZHA)"
    url: "https://www.home-assistant.io/integrations/zha/"
---

Thread and Matter get the headlines, but a huge amount of real home automation still runs on plain **Zigbee** — and the ESP32-C6 and ESP32-H2 can speak it natively. Same 2.4 GHz IEEE 802.15.4 radio that does Thread, different application stack on top. If you already have a Zigbee coordinator (a Sonoff dongle, a SkyConnect, whatever ZHA or zigbee2mqtt is talking to), an ESP32-C6 can join that network as a sensor for a couple of dollars. Here's how the pieces fit and how to ship one.

## Zigbee vs Thread/Matter, quickly

All three share the physical layer. **802.15.4** is the PHY/MAC — low-power 2.4 GHz mesh radio. On top of it:

- **Zigbee** is a full application stack (ZCL — the Zigbee Cluster Library) plus its own network layer. Devices talk to a **coordinator** that runs the network.
- **Thread** is just an IPv6 mesh network layer (6LoWPAN). It carries no application semantics on its own.
- **Matter** is the application layer that usually rides on Thread (or Wi-Fi). Matter-over-Thread is the "new" path; Zigbee is the mature one.

So Zigbee and Thread both use the C6/H2 radio, but they're mutually exclusive at runtime — you pick one stack. This article is Zigbee only.

**ESP32-C6** is a RISC-V SoC with Wi-Fi 6, Bluetooth LE 5, and 802.15.4. **ESP32-H2** drops Wi-Fi and keeps BLE + 802.15.4 — cheaper and lower-power, ideal for a battery sensor that only needs Zigbee. Espressif's **esp-zigbee-sdk** wraps the stack in a clean C API. (Historical note: the 1.x SDK was ZBOSS-based; the current 2.x line is built on ESP-IDF and Espressif's own proprietary Zigbee implementation.)

## Device roles

Zigbee has three role types, and you choose one when you init the stack:

| Role | Powers the network? | Repeats traffic? | Typical use |
|------|--------------------|--------------------|-------------|
| Coordinator | Forms it (one per network) | Yes | The dongle behind ZHA/zigbee2mqtt |
| Router | No | Yes | Mains-powered bulb, plug |
| End Device | No | No | Battery sensor (can sleep) |

For a temperature/humidity sensor you want an **End Device** (`ESP_ZB_DEVICE_TYPE_ED`) so it can sleep between reports. Your coordinator already exists as a separate dongle — you're not making the ESP32 a coordinator here.

## Endpoints, clusters, attributes

This is the mental model that makes the SDK click:

- An **endpoint** is an addressable function on the device (endpoint 10, say).
- Each endpoint exposes **clusters** — standardized feature blocks. Temperature Measurement is cluster `0x0402`; Relative Humidity Measurement is `0x0405`.
- Each cluster holds **attributes**. The Temperature Measurement cluster's `MeasuredValue` (attr `0x0000`) is a signed 16-bit int in **hundredths of a degree Celsius** — so `2350` means 23.50 °C. Humidity's `MeasuredValue` is unsigned, in 0.01 %RH.

Because these are standard ZCL clusters, a coordinator recognizes them without a custom device definition. ZHA will auto-create a sensor entity; zigbee2mqtt maps standard clusters too (fully custom clusters there need an external converter, but plain temperature/humidity doesn't).

## The code

esp-zigbee-sdk ships an `esp_zb_temperature_sensor_ep_create()` helper that builds an endpoint pre-populated with the right clusters. Add the dependency (`idf.py add-dependency "espressif/esp-zigbee-lib"`), enable the 802.15.4 radio in menuconfig, then:

```c
#include "esp_zigbee_core.h"

#define TEMP_SENSOR_EP  10

/* Build the data model and start the stack (runs in its own task) */
static void esp_zb_task(void *arg)
{
    esp_zb_cfg_t zb_cfg = {
        .esp_zb_role = ESP_ZB_DEVICE_TYPE_ED,          /* End Device */
        .install_code_policy = false,
        .nwk_cfg.zed_cfg = {
            .ed_timeout = ESP_ZB_ED_AGING_TIMEOUT_64MIN,
            .keep_alive = 3000,
        },
    };
    esp_zb_init(&zb_cfg);

    /* Standard HA temperature-sensor endpoint (Temp Measurement cluster included) */
    esp_zb_temperature_sensor_cfg_t sensor_cfg = ESP_ZB_DEFAULT_TEMPERATURE_SENSOR_CONFIG();
    esp_zb_ep_list_t *ep_list =
        esp_zb_temperature_sensor_ep_create(TEMP_SENSOR_EP, &sensor_cfg);

    esp_zb_device_register(ep_list);
    esp_zb_set_primary_network_channel_set(ESP_ZB_TRANSCEIVER_ALL_CHANNELS_MASK);
    esp_zb_start(false);
    esp_zb_stack_main_loop();      /* never returns */
}

/* Call from your sensor loop after a reading */
static void publish_temperature(int16_t centi_celsius)
{
    esp_zb_lock_acquire(portMAX_DELAY);

    /* 1) Update the local attribute */
    esp_zb_zcl_set_attribute_val(
        TEMP_SENSOR_EP,
        ESP_ZB_ZCL_CLUSTER_ID_TEMP_MEASUREMENT,
        ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
        ESP_ZB_ZCL_ATTR_TEMP_MEASUREMENT_VALUE_ID,
        &centi_celsius, false);

    /* 2) Push it to the coordinator */
    esp_zb_zcl_report_attr_cmd_t report = {
        .zcl_basic_cmd.src_endpoint = TEMP_SENSOR_EP,
        .address_mode = ESP_ZB_APS_ADDR_MODE_DST_ADDR_ENDP_NOT_PRESENT,
        .clusterID    = ESP_ZB_ZCL_CLUSTER_ID_TEMP_MEASUREMENT,
        .attributeID  = ESP_ZB_ZCL_ATTR_TEMP_MEASUREMENT_VALUE_ID,
        .direction    = ESP_ZB_ZCL_CMD_DIRECTION_TO_CLI,
    };
    esp_zb_zcl_report_attr_cmd_req(&report);

    esp_zb_lock_release();
}
```

Two things people trip on. **The SDK is not thread-safe** — any call from outside a stack callback must be wrapped in `esp_zb_lock_acquire()` / `esp_zb_lock_release()`, as above. And before `esp_zb_init()` you must call `esp_zb_platform_config()` to set the radio (native) and host connection modes, and kick network steering (`ESP_ZB_BDB_MODE_NETWORK_STEERING`) from your signal handler so the device actually joins.

Reporting has two flavors: fire `esp_zb_zcl_report_attr_cmd_req()` yourself whenever you take a reading (shown above), or configure automatic reporting so the stack sends deltas on a min/max interval. For a battery sensor, an explicit report right before sleeping is simplest and easiest to reason about.

## Build, flash, join

Pick your SoC, then build:

```bash
idf.py set-target esp32c6      # or esp32h2
idf.py -p /dev/ttyACM0 flash monitor
```

The SDK targets a recent ESP-IDF — check the repo's README for the exact version it pins. Put your coordinator into "permit join" from ZHA or zigbee2mqtt, power the ESP32, and watch the monitor for the network-steering / device-announce signals. Once it joins, ZHA discovers the Temperature Measurement cluster and a temperature entity appears — no YAML, no custom quirk needed for the standard clusters.

## Add humidity

The same endpoint can carry more. Add a Relative Humidity Measurement cluster (`ESP_ZB_ZCL_CLUSTER_ID_REL_HUMIDITY_MEASUREMENT`) to the endpoint before registering, then `set` + `report` its `MeasuredValue` the same way — feed it from a real BME280/SHT4x over I2C and you've got a two-value sensor node that any Zigbee coordinator understands.

**Try next:** deep-sleep the C6/H2 between reports and measure the join-and-report wake cost to see whether an H2 coin-cell sensor is actually viable for your reporting interval.
