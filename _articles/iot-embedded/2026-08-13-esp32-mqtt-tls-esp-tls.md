---
title: "Production TLS for ESP32 MQTT: esp-tls, the Certificate Bundle, and Mutual Auth"
date: 2026-08-13
track: iot-embedded
summary: "Moving a fleet of ESP32 nodes from plain MQTT to mqtts:// the right way: esp-tls over mbedTLS, the ESP x509 certificate bundle for broker verification, per-device client certificates for mutual TLS, what it all costs in flash and heap, and session tickets to make reconnects cheap on battery nodes. ESP-IDF v6.0.2, complete esp-mqtt config included."
reading_time: 6
tags: [esp32, esp-idf, tls, mqtt, security]
sources:
  - title: "ESP-TLS — ESP-IDF Programming Guide v6.0.2"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/protocols/esp_tls.html"
  - title: "ESP x509 Certificate Bundle — ESP-IDF Programming Guide v6.0.2"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/protocols/esp_crt_bundle.html"
  - title: "ESP-MQTT documentation (standalone component since ESP-IDF v6.0)"
    url: "https://docs.espressif.com/projects/esp-mqtt/en/latest/"
  - title: "Mbed TLS — ESP-IDF Programming Guide v6.0.2"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/protocols/mbedtls.html"
  - title: "espressif/esp-mqtt on GitHub"
    url: "https://github.com/espressif/esp-mqtt"
---

Every fleet I've run started the same way: MQTT in plaintext on a LAN, "we'll add TLS later." Later arrives the day a node has to publish across the public internet. This is the setup I now consider baseline for production, on current stable **ESP-IDF v6.0.2** — where note that esp-mqtt moved out of the IDF tree: you now pull it with `idf.py add-dependency espressif/mqtt`.

## The stack: esp-tls over mbedTLS

You rarely call mbedTLS directly on ESP-IDF. `esp-tls` is the abstraction layer that esp-mqtt, esp_http_client, and friends sit on; mbedTLS is the default engine underneath (a custom stack can be swapped in via `esp_tls_register_stack()`, but for MQTT you'll stay on mbedTLS). Everything below is configuration that flows from your `esp_mqtt_client_config_t` down through esp-tls into mbedTLS.

## Verifying the broker: use the certificate bundle

The classic mistake is embedding one server certificate and having the fleet go dark when it rotates. The right pattern for a broker behind a public CA (Let's Encrypt included) is the **ESP x509 Certificate Bundle**: a set of Mozilla NSS root certificates compiled into your firmware and attached with one function pointer. Roots rotate rarely; leaf and intermediate rotation stops being your problem.

Menuconfig gives you three sizes: the full bundle (130+ roots), the pre-selected common set (~38 roots, ~99% market coverage, meaningfully less flash), or a custom bundle via `CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE_PATH`. For a fleet, the custom option is the sleeper feature: run your own private CA, put **only that root** in the bundle, and your nodes will refuse to talk to anything but your infrastructure — smaller flash footprint and a tighter trust story than any public-CA setup.

## Mutual TLS: the broker verifies the node too

Username/password auth means a fleet-wide shared secret in every flash dump. Per-device client certificates are the grown-up answer: each node gets its own keypair and cert signed by your CA, the broker requires client auth, and revoking one stolen node doesn't touch the rest. Here's the complete config — server verification via bundle, client cert for mutual TLS:

```c
#include "esp_crt_bundle.h"
#include "mqtt_client.h"

/* EMBED_TXTFILES in CMakeLists.txt — this NUL-terminates the PEM data,
 * which esp-tls requires for PEM parsing. */
extern const uint8_t client_crt[] asm("_binary_node_crt_pem_start");
extern const uint8_t client_key[] asm("_binary_node_key_pem_start");

static void mqtt_tls_start(void)
{
    const esp_mqtt_client_config_t cfg = {
        .broker = {
            .address.uri = "mqtts://mqtt.example.com:8883",
            .verification = {
                .crt_bundle_attach = esp_crt_bundle_attach,
                /* never set .skip_cert_common_name_check in production */
            },
        },
        .credentials.authentication = {
            .certificate = (const char *)client_crt,
            .key         = (const char *)client_key,
        },
        .session.keepalive  = 60,
        .network.timeout_ms = 10000,
    };
    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&cfg);
    esp_mqtt_client_start(client);
}
```

Two gotchas from the docs that bite people: the client neither copies nor frees the cert/key buffers, so they must stay valid for the client's lifetime; and PEM buffers must be NUL-terminated, which `EMBED_TXTFILES` handles and `EMBED_FILES` does not. If your part has one, the DS peripheral (S2/S3/C3/C6) or an ATECC608 (`use_secure_element`) can hold the private key so it never sits readable in flash — pair with flash encryption otherwise, which I've covered separately.

## What TLS costs

Numbers from my S3 gateway build, so treat as order-of-magnitude: mbedTLS plus the common cert bundle adds roughly 100–150 KB of flash over a plaintext build. The painful part is heap: a full handshake transiently needs tens of kilobytes — I see peaks around 40–50 KB — dominated by the TLS record buffers, which default to 16 KB each way. The standard fix is shrinking `CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN` / `OUT_CONTENT_LEN` (the Mbed TLS docs list this first under memory optimization). 4 KB inbound works if your broker's certificate chain and records fit — test against *your* broker, because a peer sending records larger than your buffer kills the handshake with an obscure error.

```
CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y
CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_DEFAULT_CMN=y
CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN=4096
CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN=2048
CONFIG_ESP_TLS_CLIENT_SESSION_TICKETS=y
```

## Session resumption: the battery-node trick

For mains-powered nodes holding a connection, the handshake is a one-time cost. Battery nodes that wake, publish, and sleep pay it **every cycle** — and the asymmetric crypto plus extra round trips is radio-on time, which is exactly the budget you're dying on. esp-tls supports client-side session resumption: enable `CONFIG_ESP_TLS_CLIENT_SESSION_TICKETS`, grab the session after a successful connect with `esp_tls_get_client_session()`, stash it (it survives deep sleep in RTC memory or NVS), and hand it back on the next connection to skip the full handshake. The docs' phrasing — "significantly reduce the time and resources spent on full TLS handshakes" — matches what I measure: resumed connects cut TLS setup time by well over half, which compounds into real battery life at thousands of wake cycles. If both ends support TLS 1.3 (`CONFIG_MBEDTLS_SSL_PROTO_TLS1_3`), you also get a leaner handshake to begin with; check your broker's support before flipping it fleet-wide.

One honest caveat: resumption shines with esp-tls or esp_http_client, where the session API is in your hands. esp-mqtt manages its own transport internally, so for sleep-cycle MQTT nodes it's worth benchmarking whether plain reconnect cost is acceptable — or publishing the wake-up sample over HTTPS with a saved session and keeping MQTT for the always-on gateways.

**Try next:** stand up Mosquitto with a private CA (`openssl` makes this a 20-minute job), issue one per-device cert, and rebuild your node with a custom bundle containing only your root — then watch it correctly refuse a connection to any other broker.
