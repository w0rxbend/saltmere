---
title: "WireGuard on an ESP32: A VPN Tunnel for Sensor Nodes"
date: 2026-08-15
track: iot-embedded
summary: "A WireGuard tunnel gives a roaming sensor node a stable private address on the home network, keeps a network address translation (NAT) mapping alive with a 25-second keepalive, and encrypts every protocol above it, including plain MQTT. Two community ports carry it to the ESP32 — trombik's esp_wireguard for ESP-IDF and ciniml's Arduino library — at the cost of software-only ChaCha20-Poly1305, an alpha-status dependency, and a keepalive that conflicts with deep sleep."
reading_time: 6
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

**Gist.** A sensor node deployed on a network its operator does not control has no reachable address and no trusted path to the broker; per-connection Transport Layer Security (TLS) encrypts the payload but does not solve addressing or reachability. WireGuard replaces the problem with a virtual network interface: the node holds a fixed private address such as `10.6.0.12`, and every protocol above the interface — including plain-TCP MQTT — travels inside an encrypted User Datagram Protocol (UDP) flow to a peer the operator runs. The cost is a software-only cipher on the Xtensa core, a third-party alpha-status component, and a keepalive interval that bounds how long the radio may sleep.

The corpus already covers [MQTT over TLS with esp-tls](/articles/iot-embedded/2026-08-13-esp32-mqtt-tls-esp-tls/), which secures a single connection at the application layer. WireGuard operates one layer down and secures the interface instead. A node behind a friend's router, a phone hotspot, or a campus NAT still presents the same tunnel address and publishes to the same broker address as a node on the bench.

## Two ports of the same lwIP implementation

No official WireGuard implementation targets microcontrollers. What runs on the ESP32 descends from Daniel Hope's WireGuard implementation for lwIP, the Transmission Control Protocol/Internet Protocol (TCP/IP) stack that ESP-IDF uses. Two wrappers exist:

- **[trombik/esp_wireguard](https://github.com/trombik/esp_wireguard)** — an ESP-IDF component under BSD-3-Clause, published on the [ESP Component Registry as v0.9.0](https://components.espressif.com/components/trombik/esp_wireguard). It targets ESP32, ESP32-S2 and ESP32-C3, and also the ESP8266 RTOS SDK. The README states the constraints plainly: **alpha status, a single tunnel to a single peer, Wi-Fi station mode only, IPv6 untested.** Its declared IDF support tops out at v4.4-era releases and the repository has been quiet since, so a build against a current IDF should be expected to need fixes; the registry lists forks.
- **[ciniml/WireGuard-ESP32-Arduino](https://github.com/ciniml/WireGuard-ESP32-Arduino)** — the same lwIP core adapted to Arduino-ESP32, originally demonstrated against SORACOM Arc. It is the shorter path for firmware already built on the Arduino core.

Neither README publishes throughput or RAM figures, so numbers quoted elsewhere are unverified and must be measured on the target hardware. One qualitative bound is firm: **WireGuard's cipher suite is ChaCha20-Poly1305, which the ESP32's AES accelerator cannot serve, so encryption runs entirely in software on the Xtensa core.** That is adequate for a telemetry payload every ten seconds and inadequate for a camera stream.

## The wall-clock precondition

WireGuard's handshake carries a timestamp, and the responder rejects a handshake whose timestamp does not exceed the greatest one seen from that peer. The consequence on a microcontroller with no battery-backed clock is direct: **a node whose clock has not been set will produce handshakes the server discards, and the tunnel never establishes.** [Simple Network Time Protocol (SNTP) synchronisation](/articles/iot-embedded/2026-08-15-esp32-time-sync-sntp-deep-sleep/) must therefore complete before `esp_wireguard_connect` is called, not concurrently with it. The failure presents as a silent connect that never carries traffic rather than as an error return, because the initiator has no way to distinguish a discarded handshake from a lost packet.

## Configuration

The ESP-IDF component mirrors a standard `wg0.conf` in a struct:

```c
#include "esp_wireguard.h"

static wireguard_ctx_t ctx = {0};

void start_wireguard(void)
{
    // Precondition: Wi-Fi is associated and SNTP has set the system clock.
    wireguard_config_t wg = ESP_WIREGUARD_CONFIG_DEFAULT();
    wg.private_key       = CONFIG_WG_PRIVATE_KEY;     // this node's key
    wg.public_key        = CONFIG_WG_PEER_PUBLIC_KEY; // server's key
    wg.address           = "10.6.0.12";               // node's tunnel address
    wg.netmask           = "255.255.255.0";
    wg.endpoint          = "vpn.example.org";
    wg.port              = 51820;
    wg.persistent_keepalive = 25;

    ESP_ERROR_CHECK(esp_wireguard_init(&wg, &ctx));
    ESP_ERROR_CHECK(esp_wireguard_connect(&ctx));
}
```

On the server the node is one more `[Peer]` with `AllowedIPs = 10.6.0.12/32`. Keys are generated with `wg genkey | tee private | wg pubkey > public`. The private key is a credential for the whole tunnel, so a node recovered from the field yields network access unless the key is protected: [flash encryption](/articles/iot-embedded/2026-07-30-esp32-secure-boot-flash-encryption/) is what separates a stolen device from the VPN.

## Traffic profile and its effect on a battery node

Three components generate traffic, and only one of them is under configuration control:

- **Handshake.** WireGuard performs an elliptic-curve Diffie-Hellman exchange over Curve25519, rekeying on the order of every two minutes while traffic flows. It is initiated lazily: with no traffic to send, no rekey occurs. On an ESP32 the exchange costs a pair of UDP datagrams and several Curve25519 operations in software; no published measurement fixes the CPU cost on this hardware.
- **Persistent keepalive.** The [WireGuard quick start](https://www.wireguard.com/quickstart/) recommends a 25-second persistent keepalive for a peer behind NAT, which is the `25` above. It emits a small UDP datagram every 25 seconds, and the implication for power is the load-bearing one: **a node that must keep its NAT mapping alive cannot let its radio sleep longer than the keepalive interval.** For a mains-powered node this is immaterial. For a [deep-sleeping battery node](/articles/iot-embedded/2026-07-26-esp32-deep-sleep-power/) it is decisive — the workable configuration is to disable persistent keepalive entirely, allow the tunnel to lapse between reports, and pay a handshake round trip on the first packet after wake, which lengthens the awake window by one network round trip plus the handshake computation.
- **Per-packet overhead.** WireGuard's data-message framing adds 32 bytes — a 16-byte header plus the 16-byte Poly1305 tag — and over IPv4 the outer UDP and IP headers add 28 more, for an envelope of 60 bytes per datagram. Batching several readings into one publish amortises it.

## WireGuard against MQTT over TLS

The [esp-tls route](/articles/iot-embedded/2026-08-13-esp32-mqtt-tls-esp-tls/) requires no server-side VPN, uses the ESP32's hardware AES and SHA accelerators, and its handshake heap is held only while connecting. It secures exactly one connection and requires the broker to be reachable from wherever the node sits, which on a foreign network means exposing the broker to the internet.

WireGuard inverts the trade. **One tunnel secures every protocol the node speaks** — plain-TCP MQTT, over-the-air firmware fetches, a debug shell — the broker stays on a private address, and a roaming node keeps a fixed address across networks. The costs are an alpha-status third-party component, a cipher with no hardware acceleration, and a keepalive that conflicts with deep sleep. The split that follows from those constraints: TLS for battery nodes on a controlled LAN, WireGuard for mains-powered nodes deployed on networks the operator does not own.

## Pitfalls

- **The tunnel initialises without error but carries no traffic.** The system clock is unset or stale, so the handshake timestamp fails the responder's monotonicity check and the handshake is discarded; SNTP must complete before the connect call.
- **A deep-sleep cycle longer than the keepalive interval strands the node.** The NAT mapping on the intervening router expires while the radio is off, and the server, which knows the peer only by its last observed endpoint, has no route to reach it until the node initiates again.
- **A second peer is configured and one of them never connects.** trombik/esp_wireguard documents support for a single tunnel to a single peer; a multi-peer topology is outside what the component provides.
- **A build against a current ESP-IDF fails to compile.** The component's declared support stops at v4.4-era releases, and no upstream update has followed.
- **Camera or bulk transfers stall.** ChaCha20-Poly1305 runs in software because the AES accelerator does not apply to it, so throughput is bounded by the Xtensa core rather than by the radio.
- **A recovered node yields network access.** The private key sits in flash, and without flash encryption it can be read off the device and used to impersonate the peer.
- **IPv6 addressing behaves unpredictably.** The README lists IPv6 as untested rather than supported.
