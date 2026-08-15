---
title: "ESP-NOW for Battery Sensor Nodes: Wake, Transmit, Sleep"
date: 2026-07-27
track: iot-embedded
summary: "ESP-NOW's connectionless frames against Wi-Fi plus MQTT for battery air-quality nodes: payload and peer limits, the channel constraint, an ESP-IDF send/receive pair, and a gateway that bridges to MQTT."
reading_time: 7
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

**Gist.** A battery-powered sensor node that reports once a minute over conventional Wi-Fi must associate with an access point (AP), obtain an address by Dynamic Host Configuration Protocol (DHCP), open a Transmission Control Protocol (TCP) socket and complete a Message Queuing Telemetry Transport (MQTT) handshake before the first measurement leaves the board — a multi-step exchange whose energy cost can exceed the measurement itself. ESP-NOW removes that stack: it is a connectionless protocol carried in Wi-Fi action frames, addressed by peer Media Access Control (MAC) address, so a node can wake, transmit one frame and sleep without any association. The cost is that everything the discarded stack provided must be supplied elsewhere — there is no routing, no fragmentation beyond a small frame limit, no channel negotiation, and no delivery guarantee past a single MAC-layer acknowledgement.

The [SEN5x + MQTT article](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt/) describes the association-based path. It suits a mains-powered gateway. It suits a coin-cell or 18650 node poorly.

## What ESP-NOW is

ESP-NOW is Espressif's connectionless Wi-Fi protocol. It uses the Wi-Fi radio and Wi-Fi action frames, but performs no association, requires no AP, and carries no Internet Protocol (IP) stack. The application supplies a peer MAC address and a buffer; the driver places a short frame on the air. **Because there is no handshake, the awake window is bounded by the time to configure the radio and transmit one frame, rather than by a multi-round association and address-assignment exchange.** That difference in awake time is the reason the protocol suits battery sensors.

The protocol is supported across the ESP32 family (ESP32, S2, S3, C3, C6 and newer parts) and on ESP8266, so an inexpensive node and a larger gateway part can interoperate.

## Payload and peer limits

Per the [ESP-IDF ESP-NOW documentation](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/network/esp_now.html), the classic maximum payload is **250 bytes** (`ESP_NOW_MAX_DATA_LEN`). A packed structure holding a few floats, a battery voltage and a sequence counter is far below that bound. Devices implementing v2.0 raise the ceiling to **1490 bytes** (`ESP_NOW_MAX_DATA_LEN_V2`), but a v1.0 peer still caps a single frame at 250 bytes, so a payload that must reach mixed peers is constrained by the lower figure.

Two further limits shape the topology: **at most 20 peers in total, of which at most 17 may use encryption** (the default configuration allows 7). A frame is either unicast to one peer's MAC address or broadcast to `ff:ff:ff:ff:ff:ff`. **Broadcast frames are neither acknowledged nor encrypted;** unicast frames yield a per-send delivery status in the send callback. One-to-many telemetry therefore trades away the only delivery signal the protocol offers.

## The channel constraint

ESP-NOW performs no channel negotiation. Passing channel `0` to `esp_now_add_peer` means the frame is transmitted on whichever channel the radio currently occupies; any other value must match the local device's channel. The invariant is blunt: **sender and receiver must be on the same Wi-Fi channel at the moment of transmission, and nothing in the protocol discovers or corrects a mismatch.** The failure mode is silent. A node on channel 1 sending to a gateway parked on channel 6 receives no error at the application programming interface (API) level; the send callback reports failure only for unicast, and the frames are never heard.

This binds the gateway design. A gateway that also associates with a router inherits the router's channel, and every sensor node must transmit on that same channel. A router that performs automatic channel selection can move that channel without notice.

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

Two details in that listing are load-bearing. **Station mode must be started but must not associate** — the radio is required, the association is not. And the deep-sleep call sits in the send callback rather than after `esp_now_send`, because the send call returns before the frame has been handed to the air.

## The wake cycle as a state machine

There is no connection to keep alive, so the cycle is stateless and linear: **the deep-sleep timer wakes the chip, `app_main` runs from the top, one frame is queued, the send callback reports the MAC-layer acknowledgement, and the node re-enters deep sleep.** Deep sleep does not preserve ordinary random-access memory, so nothing survives a cycle except what is written to real-time clock (RTC) memory. A sequence counter kept there lets the gateway detect gaps, which is the only drop signal available for broadcast traffic and a cross-check on the acknowledgement for unicast.

The association-per-wake pattern discussed in the [ESP32 deep-sleep article](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power/) keeps the radio powered for a multi-step handshake. Here the radio is powered for one frame. The saving is proportional to the difference in awake time, which is why the measurement below is the one worth taking.

## The gateway: ESP-NOW to MQTT

The gateway is a mains-powered ESP32 that does not sleep. It runs Wi-Fi in station mode, stays associated with the router, and has ESP-NOW available on that same channel. Its receive callback unpacks each structure and republishes the reading over MQTT, reusing the broker and topic scheme from the SEN5x article.

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

The callback is registered with `esp_now_register_recv_cb(on_recv)` after `esp_now_init()`. **The length check is the only validation an unencrypted ESP-NOW receiver gets for free:** any device within range can transmit a frame to a known MAC address, and a frame of the wrong size is the cheapest thing to reject. The source MAC address in `src_addr` supplies the node identity used in the topic, which removes any need to provision identifiers on the nodes.

A useful measurement: place a multimeter or power profiler in series with the node's battery and compare total charge (mAh) per wake cycle against the same reading delivered over associated Wi-Fi and MQTT.

## Pitfalls

- **A channel mismatch produces no error and no log line.** The receiving radio never sees the frame, and a broadcast sender has no acknowledgement to fail on, so the gateway stays quiet; the symptom is a node that appears healthy and a topic that never updates.
- **A router with automatic channel selection breaks a working deployment overnight.** The gateway follows the router to the new channel; the nodes, which hard-code a channel, do not.
- **Calling `esp_deep_sleep` immediately after `esp_now_send` can cut the transmission.** `esp_now_send` returns before the frame is on the air, so the sleep transition must be driven from the send callback.
- **Broadcast frames carry no acknowledgement and no encryption.** A design that switches from unicast to broadcast for fan-out silently loses its only per-frame delivery signal.
- **Exceeding 250 bytes breaks interoperability rather than failing loudly on the sender.** A v2.0 sender may accept a payload up to 1490 bytes that a v1.0 peer cannot receive.
- **The peer table is finite.** With a maximum of 20 peers, and at most 17 encrypted, a deployment that grows past those counts requires broadcast or multiple gateways rather than more `esp_now_add_peer` calls.
- **Values in ordinary RAM do not survive deep sleep.** A sequence counter kept outside RTC memory restarts at its initial value every cycle, making drop detection at the gateway meaningless.
