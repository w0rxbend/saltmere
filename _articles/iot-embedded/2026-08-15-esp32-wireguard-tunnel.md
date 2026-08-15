---
title: "WireGuard on an ESP32: A VPN Tunnel for Sensor Nodes"
date: 2026-08-15
track: iot-embedded
summary: "A WireGuard tunnel gives a roaming sensor node a stable private IP back to your home server, punches through NAT with a 25-second keepalive, and encrypts everything above it — including plain MQTT. Two community ports make it work on ESP32 (trombik's esp_wireguard for ESP-IDF, ciniml's Arduino library); here's how they fit, what they cost on battery, and when per-connection TLS is still the better call."
reading_time: 5
tags: [esp32, wireguard, vpn, mqtt, esp-idf, networking, security]
sources:
  - title: "trombik/esp_wireguard — WireGuard Implementation for ESP-IDF (GitHub)"
    url: "https://github.com/trombik/esp_wireguard"
  - title: "trombik/esp_wireguard v0.9.0 — ESP Component Registry"
    url: "https://components.espressif.com/components/trombik/esp_wireguard"
  - title: "ciniml/WireGuard-ESP32-Arduino — README (GitHub)"
    url: "https://github.com/ciniml/WireGuard-ESP32-Arduino/blob/main/README.md"
  - title: "WireGuard — Quick Start (persistent keepalive, NAT traversal)"
    url: "https://www.wireguard.com/quickstart/"
---

The corpus already covers [MQTT over TLS with esp-tls](/articles/iot-embedded/2026-08-13-esp32-mqtt-tls-esp-tls/) — per-connection encryption, done at the application layer. WireGuard solves a different-shaped problem: instead of securing *one connection*, it gives the node a network interface with a stable private IP that works from any network. Put a node at a friend's house, on cellular via a phone hotspot, or behind a hostile campus NAT, and it still appears as `10.6.0.12` on your home LAN, publishing to the same broker address as the node on your desk. That's the actual pitch for a sensor fleet: NAT traversal and addressing, with encryption as a bonus.

## Two ports of the same lwIP implementation

There's no official WireGuard for microcontrollers. What exists on ESP32 descends from Daniel Hope's WireGuard implementation for lwIP (the TCP/IP stack ESP-IDF uses), wrapped two ways:

- **[trombik/esp_wireguard](https://github.com/trombik/esp_wireguard)** — a proper ESP-IDF component, BSD-3-Clause, published on the [ESP Component Registry as v0.9.0](https://components.espressif.com/components/trombik/esp_wireguard). Supports ESP32/S2/C3 (and even ESP8266 RTOS SDK). The README is upfront about its status: alpha, a single tunnel to a single peer, Wi-Fi station only, IPv6 untested. The listed IDF support tops out at v4.4-era releases and activity has been quiet, so budget an afternoon for build fixes on current IDF — or look at the registry for maintained forks.
- **[ciniml/WireGuard-ESP32-Arduino](https://github.com/ciniml/WireGuard-ESP32-Arduino)** — the same lwIP core adapted for Arduino-ESP32, originally demoed against SORACOM Arc. If your node is Arduino-core or ESPHome-adjacent, this is the shorter path.

Neither project publishes throughput or RAM benchmarks in its README, so treat any numbers you see quoted elsewhere with suspicion and measure on your own hardware. Qualitatively: ChaCha20-Poly1305 runs in software on the Xtensa core (the ESP32's AES accelerator doesn't help WireGuard), which is fine for sensor telemetry — a JSON payload every 10 seconds is nothing — and not fine if you imagined tunneling camera streams.

## Configuration

The ESP-IDF component mirrors a standard `wg0.conf` in a struct. One thing bites everyone: **WireGuard requires valid wall-clock time** for handshake timestamps (it's how the protocol rejects replayed handshakes), so [SNTP sync](/articles/iot-embedded/2026-08-15-esp32-time-sync-sntp-deep-sleep/) must complete before you connect:

```c
#include "esp_wireguard.h"

static wireguard_ctx_t ctx = {0};

void start_wireguard(void)
{
    // Wi-Fi is up and SNTP has synced before this point.
    wireguard_config_t wg = ESP_WIREGUARD_CONFIG_DEFAULT();
    wg.private_key       = CONFIG_WG_PRIVATE_KEY;    // this node's key
    wg.public_key        = CONFIG_WG_PEER_PUBLIC_KEY; // home server's key
    wg.address           = "10.6.0.12";              // node's tunnel IP
    wg.netmask           = "255.255.255.0";
    wg.endpoint          = "vpn.example.org";        // home server, port 51820
    wg.port              = 51820;
    wg.persistent_keepalive = 25;

    ESP_ERROR_CHECK(esp_wireguard_init(&wg, &ctx));
    ESP_ERROR_CHECK(esp_wireguard_connect(&ctx));
}
```

On the server side the node is just another `[Peer]` with `AllowedIPs = 10.6.0.12/32`. Generate keys with the normal `wg genkey | tee private | wg pubkey > public` and bake them in via NVS or Kconfig — not hardcoded, since [flash encryption](/articles/iot-embedded/2026-07-30-esp32-secure-boot-flash-encryption/) is the only thing standing between a stolen node and your VPN.

## What it costs on battery

WireGuard's traffic profile has three components, and only one is optional:

- **Handshake**: an ECDH exchange (Curve25519) roughly every two minutes of active traffic, initiated lazily — no traffic, no rekey. A few hundred milliseconds of CPU on an ESP32, a couple of small UDP packets.
- **Persistent keepalive**: the `25` in the config above, the value the [WireGuard docs](https://www.wireguard.com/quickstart/) suggest for peers behind NAT. That's a tiny UDP packet every 25 s — which means *your radio can never sleep longer than 25 s* without the NAT mapping going stale. For a mains-powered node, irrelevant. For a [deep-sleeping battery node](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power/), this is the dealbreaker setting: drop persistent keepalive entirely, accept that the first packet after wake pays a handshake round-trip (add ~1–2 s to your awake window), and let the tunnel die between reports.
- **Per-packet overhead**: 32 bytes of WireGuard framing plus outer UDP/IP — around 60 bytes per packet, trivially amortized by batching readings.

## WireGuard vs. MQTT-TLS: pick per node

The [esp-tls article](/articles/iot-embedded/2026-08-13-esp32-mqtt-tls-esp-tls/) route — TLS 1.2/1.3 straight to the broker — needs no server-side VPN, uses the hardware AES/SHA accelerators, and its ~40 KB of handshake heap is paid only while connecting. It secures exactly one connection, though, and requires the broker to be reachable, which behind someone else's NAT means exposing it to the internet. WireGuard inverts the trade: one tunnel secures *everything* (plain-TCP MQTT, OTA fetches, a debug telnet), the broker stays private, and roaming nodes keep a fixed address — at the price of an alpha-status third-party component and a keepalive that fights deep sleep. The pragmatic split: TLS for battery nodes on your own LAN, WireGuard for mains-powered nodes deployed on networks you don't control.

**Try next:** add a `wg0` peer for one mains-powered node, point its MQTT client at the broker's tunnel IP with plain TCP, and compare heap watermarks and reconnect latency against the same firmware running mutual-TLS — then unplug it, tether it to a phone hotspot, and watch it come back on the same IP.
