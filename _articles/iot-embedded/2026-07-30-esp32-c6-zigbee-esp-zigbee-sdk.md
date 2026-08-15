---
title: "Zigbee on the ESP32-C6/H2 with esp-zigbee-sdk: a temperature sensor from scratch"
date: 2026-07-30
track: iot-embedded
summary: "Building a Zigbee end device on the ESP32-C6 or ESP32-H2: the IEEE 802.15.4 radio, device roles, the endpoint/cluster/attribute model, and attribute reporting to Home Assistant's ZHA integration."
reading_time: 6
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

**Gist.** A large installed base of home automation runs on Zigbee rather than Thread or Matter, so a new sensor node has to speak the Zigbee application stack to be discovered by an existing coordinator. The ESP32-C6 and ESP32-H2 carry an IEEE 802.15.4 radio and Espressif's esp-zigbee-sdk supplies the stack above it, so a device that publishes a standard Zigbee Cluster Library (ZCL) cluster is recognised without a custom device definition. The cost is exclusivity and discipline: the radio serves one stack at a time, so a Zigbee build cannot also be a Thread build, and every stack call made outside a stack callback must be taken under the SDK's lock.

## Zigbee, Thread and Matter on one radio

All three rest on the same physical layer. **IEEE 802.15.4 is the PHY and MAC** — a low-power 2.4 GHz radio with no routing of its own. Meshing belongs to the layers above it, and those layers differ:

- **Zigbee** is a complete application stack — the Zigbee Cluster Library (ZCL) — together with its own network layer. Devices attach to a **coordinator**, which forms and runs the network.
- **Thread** is an IPv6 mesh network layer over 6LoWPAN. It carries no application semantics.
- **Matter** is an application layer carried over another transport, Thread or Wi-Fi among them.

Zigbee and Thread both drive the C6/H2 radio, and **they are mutually exclusive at runtime**: a firmware image selects one stack. The material below covers Zigbee only.

**ESP32-C6** is a RISC-V system-on-chip (SoC) with Wi-Fi 6, Bluetooth Low Energy (BLE) 5 and 802.15.4. **ESP32-H2** omits Wi-Fi and retains BLE and 802.15.4, which suits a battery node that needs no IP connectivity of its own. The esp-zigbee-sdk exposes the stack through a C application programming interface (API). The SDK has had more than one underlying stack across its releases, so the component set a project depends on follows the SDK version rather than the other way round; the repository records which components each release requires.

## Device roles

A role is fixed at stack initialisation:

| Role | Forms the network? | Repeats traffic? | Typical use |
|------|--------------------|--------------------|-------------|
| Coordinator | Yes, one per network | Yes | The dongle behind ZHA or zigbee2mqtt |
| Router | No | Yes | Mains-powered bulb or plug |
| End Device | No | No | Battery sensor, may sleep |

A temperature or humidity node is an **End Device** (`ESP_ZB_DEVICE_TYPE_ED`), the only role permitted to sleep, because a role that repeats traffic must keep its receiver on to forward frames for neighbours. The coordinator is a separate device already present on the network.

## Endpoints, clusters, attributes

The data model is three levels deep, and the SDK's API mirrors it exactly.

- An **endpoint** is an addressable function on a device, identified by a small integer.
- Each endpoint exposes **clusters**, the standardised feature blocks of the ZCL. Temperature Measurement is cluster `0x0402`; Relative Humidity Measurement is `0x0405`.
- Each cluster holds **attributes**. Temperature Measurement's `MeasuredValue` (attribute `0x0000`) is a **signed 16-bit integer in hundredths of a degree Celsius**, so the encoding of 23.50 °C is `2350`. Humidity's `MeasuredValue` is unsigned, in units of 0.01 %RH.

Because these are standard clusters, a coordinator interprets them without device-specific configuration. ZHA creates a sensor entity on discovery; zigbee2mqtt likewise maps standard clusters, whereas fully custom clusters there require an external converter.

Each cluster instance also carries a **role**: a server holds the attribute values, a client reads or receives them. A sensor's Temperature Measurement cluster is therefore the **server** side (`ESP_ZB_ZCL_CLUSTER_SERVER_ROLE`), and its reports travel toward the client (`ESP_ZB_ZCL_CMD_DIRECTION_TO_CLI`).

## The code

The SDK provides `esp_zb_temperature_sensor_ep_create()`, which builds an endpoint pre-populated with the clusters of the standard temperature-sensor device. Add the dependency with `idf.py add-dependency "espressif/esp-zigbee-lib"`, enable the 802.15.4 radio in menuconfig, then:

```c
#include "esp_zigbee_core.h"

#define TEMP_SENSOR_EP  10

/* Build the data model and start the stack (runs in its own task;
   esp_zb_platform_config() has already run in app_main) */
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

/* Called from the sensor loop after a reading */
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

Three invariants hold this together. **The SDK is not thread-safe**: any call issued from outside a stack callback must be bracketed by `esp_zb_lock_acquire()` and `esp_zb_lock_release()`. **`esp_zb_platform_config()` must precede `esp_zb_init()`**, setting the radio mode (native, for the on-chip 802.15.4 transceiver) and the host connection mode. **Joining is not automatic**: the application starts Base Device Behaviour network steering (`ESP_ZB_BDB_MODE_NETWORK_STEERING`) from its signal handler, and without that step the device never attaches to a network.

Note the separation between the two calls in `publish_temperature`. `esp_zb_zcl_set_attribute_val()` writes the local attribute store, which is what a coordinator sees when it issues a read; `esp_zb_zcl_report_attr_cmd_req()` transmits. Reporting has two forms — an explicit report command per reading, as shown, or automatic reporting configured with minimum and maximum intervals so the stack emits on change and on timeout. An explicit report immediately before entering sleep keeps the transmit instant under application control.

## Build, flash, join

```bash
idf.py set-target esp32c6      # or esp32h2
idf.py -p /dev/ttyACM0 flash monitor
```

The SDK targets a recent ESP-IDF; the repository README records the exact version it pins. The coordinator must be placed in "permit join" from ZHA or zigbee2mqtt before the node is powered, and the serial monitor reports the network-steering and device-announce signals as they occur. After the join, ZHA discovers the Temperature Measurement cluster and creates a temperature entity; standard clusters need no YAML and no custom quirk.

## Adding humidity

One endpoint can carry several clusters. Adding a Relative Humidity Measurement cluster (`ESP_ZB_ZCL_CLUSTER_ID_REL_HUMIDITY_MEASUREMENT`) to the endpoint **before `esp_zb_device_register()`** yields a second attribute, set and reported by the same pair of calls, fed from a sensor such as a BME280 or SHT4x over I2C.

## Pitfalls

- A stack call made from an application task without `esp_zb_lock_acquire()` corrupts stack state or crashes, because the SDK is not thread-safe and the application task races the stack task.
- Omitting `esp_zb_platform_config()` before `esp_zb_init()` leaves the radio and host connection modes unset, so the stack initialises without a usable transceiver.
- A device that never issues `ESP_ZB_BDB_MODE_NETWORK_STEERING` from its signal handler stays unjoined indefinitely and shows no error: the stack is running but has no network.
- Writing a temperature in whole degrees into `MeasuredValue` under-reports by a factor of 100, since the attribute is defined in hundredths of a degree Celsius.
- Passing an unsigned value to Temperature Measurement's `MeasuredValue` mis-encodes readings below 0 °C, because that attribute is signed while Relative Humidity's is unsigned.
- Registering clusters after `esp_zb_device_register()` leaves them absent from the announced descriptor, so the coordinator discovers only the clusters present at registration time.
- Building the Thread stack and the Zigbee stack into one image gains nothing usable: the radio serves one stack at a time.
