---
title: "ESP-NOW for Battery Sensor Nodes: Wake, Blast, Sleep"
date: 2026-07-27
track: iot-embedded
summary: "Why ESP-NOW's connectionless frames beat Wi-Fi + MQTT for battery air-quality nodes, with an ESP-IDF send/recv example and a gateway that bridges to MQTT."
reading_time: 5
tags: [esp32, esp-now, low-power, deep-sleep, mqtt, sensors]
sources:
  - title: "ESP-NOW — ESP-IDF Programming Guide (stable, ESP32)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/network/esp_now.html"
  - title: "espressif/esp-now — User_Guide.md"
    url: "https://github.com/espressif/esp-now/blob/master/User_Guide.md"
  - title: "Getting Started with ESP-NOW (ESP32 with Arduino IDE) — Random Nerd Tutorials"
    url: "https://randomnerdtutorials.com/esp-now-esp32-arduino-ide/"
  - title: "Using ESP-NOW in Arduino — Espressif Developer Portal"
    url: "https://developer.espressif.com/blog/2024/08/arduino-esp-now-lib/"
---

In the [SEN5x + MQTT article](/articles/iot-embedded/sen5x-mqtt/) the node associated with an access point, waited for DHCP, opened a TCP socket, and did an MQTT handshake before a single PM2.5 reading left the board. For a mains-powered gateway that is fine. For a coin-cell or 18650 air-quality node that wakes once a minute, the association and DHCP dance can burn more energy than the measurement itself. ESP-NOW removes that whole stack.

## What ESP-NOW actually is

ESP-NOW is Espressif's connectionless Wi-Fi protocol. It rides on the Wi-Fi radio using action frames, but there is no association, no AP, no DHCP, and no TCP/IP. You hand it a peer MAC address and a buffer; it puts a short frame on the air. Because there is no handshake, a node can boot from deep sleep, transmit a reading, and go back to sleep in a few milliseconds instead of the hundreds of milliseconds a full Wi-Fi association takes. That latency delta is the entire reason ESP-NOW fits battery sensors.

It is supported across the ESP32 family (ESP32, S2, S3, C3, C6, and newer parts) and even ESP8266, so a cheap node and a beefier gateway can talk.

## Payload limits and peers

Per the [ESP-IDF ESP-NOW docs](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/network/esp_now.html), the classic maximum payload is **250 bytes** (`ESP_NOW_MAX_DATA_LEN`). This is the number to design around today, and it is plenty: a struct with a few floats, a battery voltage, and a sequence counter is well under it. Newer v2.0 devices raise the ceiling to **1470 bytes** (`ESP_NOW_MAX_DATA_LEN_V2`), but v1.0 peers still cap a single frame at 250, so keep sensor payloads small for interoperability.

Other limits worth knowing: up to **20 total peers**, of which no more than **17** can use encryption (default 7). A frame can be unicast to one peer's MAC or broadcast to `ff:ff:ff:ff:ff:ff` (no ack, no encryption). Broadcast is handy for one-to-many telemetry; unicast gives you a per-send delivery status in the callback.

## Channel constraint

ESP-NOW has no channel negotiation. If you pass channel `0` to `esp_now_add_peer`, the frame goes out on whatever channel the radio is currently on. Otherwise the peer's channel must match the local device's channel. This matters for the gateway (below): once it connects to your home Wi-Fi, the router pins its channel, and every sensor node must transmit on that same channel or the frames are never heard.

## A sensor node in ESP-IDF

```c
#include "esp_now.h"
#include "esp_wifi.h"
#include "esp_sleep.h"
#include <string.h>

typedef struct {
    uint16_t seq;
    float    pm25;
    float    temp_c;
    float    vbat;
} __attribute__((packed)) reading_t;   // 14 bytes, well under 250

static const uint8_t GATEWAY_MAC[6] =
    {0x24, 0x6F, 0x28, 0x00, 0x11, 0x22};

static void on_sent(const uint8_t *mac, esp_now_send_status_t status) {
    // ESP_NOW_SEND_SUCCESS means the MAC-layer ack came back
    esp_deep_sleep(60ULL * 1000000ULL);   // sleep 60 s after TX
}

void app_main(void) {
    // Wi-Fi must be started (STA) but NOT associated to any AP
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_start();
    esp_wifi_set_channel(1, WIFI_SECOND_CHAN_NONE);  // match gateway

    esp_now_init();
    esp_now_register_send_cb(on_sent);

    esp_now_peer_info_t peer = { 0 };
    memcpy(peer.peer_addr, GATEWAY_MAC, 6);
    peer.channel = 0;      // use current channel
    peer.encrypt = false;
    esp_now_add_peer(&peer);

    reading_t r = { .seq = 42, .pm25 = 8.3f, .temp_c = 21.7f, .vbat = 3.91f };
    esp_now_send(GATEWAY_MAC, (uint8_t *)&r, sizeof(r));
    // control returns via on_sent(), which triggers deep sleep
}
```

## How deep sleep rewrites the flow

There is no persistent connection to keep alive, so the loop is stateless: **deep-sleep timer wakes the chip, `app_main` runs top to bottom, one frame goes out, the send callback confirms the ack, and the node sleeps again.** Nothing is retained between cycles except what you stash in RTC memory (a sequence counter is worth keeping there to detect drops). Contrast this with the association-per-wake pattern from the [ESP32 deep-sleep article](/articles/iot-embedded/esp32-deep-sleep/): here the radio is on for a single frame, not a multi-step handshake, so the awake window shrinks dramatically. Don't block waiting in `app_main` after `esp_now_send`; let the callback drive the transition to sleep so you never sleep before the frame is actually transmitted.

## The gateway: ESP-NOW to MQTT

The gateway is a mains-powered ESP32 that never sleeps. It runs Wi-Fi in station mode and stays connected to your router, so it also has ESP-NOW available on that same channel. Its receive callback unpacks each struct and republishes it over MQTT, reusing the broker and topic scheme from the SEN5x article.

```c
static void on_recv(const esp_now_recv_info_t *info,
                    const uint8_t *data, int len) {
    if (len != sizeof(reading_t)) return;   // reject malformed frames
    reading_t r;
    memcpy(&r, data, sizeof(r));
    char topic[64], payload[64];
    snprintf(topic, sizeof(topic), "air/%02x%02x%02x/pm25",
             info->src_addr[3], info->src_addr[4], info->src_addr[5]);
    snprintf(payload, sizeof(payload), "%.1f", r.pm25);
    esp_mqtt_client_publish(client, topic, payload, 0, 1, 0);
}
```

Register it with `esp_now_register_recv_cb(on_recv)` after `esp_now_init()`. The sensor MAC in `src_addr` becomes the node identity in the topic, so you never have to configure IDs on the nodes themselves. The result: nodes that spend 99% of their life asleep, a gateway that turns their frames into normal MQTT the rest of your stack already understands.

**Try next:** Flash the node and gateway, put a multimeter or a power profiler in series with the node's battery, and compare the total charge (mAh) per wake cycle against the same reading sent over full Wi-Fi + MQTT — the ESP-NOW path should be a fraction of the association-based one.
