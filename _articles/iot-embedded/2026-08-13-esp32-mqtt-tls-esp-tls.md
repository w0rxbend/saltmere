---
title: "Production TLS for ESP32 MQTT: esp-tls, the Certificate Bundle, and Mutual Auth"
date: 2026-08-13
track: iot-embedded
summary: "Moving a fleet of ESP32 nodes from plain MQTT to mqtts://: esp-tls over mbedTLS, the ESP x509 certificate bundle for broker verification, per-device client certificates for mutual TLS, the flash and heap cost, and session tickets that make reconnects cheaper on battery nodes. ESP-IDF v6.0.2, with a complete esp-mqtt configuration."
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

**Gist.** Message Queuing Telemetry Transport (MQTT) over a plaintext socket exposes credentials and payloads to anyone on the path, and the exposure becomes unacceptable the moment a node publishes across the public internet. Transport Layer Security (TLS) on ESP-IDF closes that gap through `esp-tls`, an abstraction layer over mbedTLS that supplies root-certificate verification via the ESP x509 certificate bundle and per-device client certificates for mutual authentication. The mechanism is not free: it adds roughly 100–150 KB of flash and a transient handshake heap peak in the tens of kilobytes, and each wake cycle on a battery node pays the handshake's asymmetric cryptography and extra round trips in radio-on time.

The configuration below targets current stable **ESP-IDF v6.0.2**. Note that esp-mqtt is no longer in the IDF tree; it is pulled as a managed component with `idf.py add-dependency espressif/mqtt`.

## The stack: esp-tls over mbedTLS

Application code on ESP-IDF rarely calls mbedTLS directly. `esp-tls` is the abstraction layer on which esp-mqtt and `esp_http_client` sit, and **mbedTLS is the default engine underneath**. An alternative stack can be registered with `esp_tls_register_stack()`, but the MQTT path documented here stays on mbedTLS. Every setting discussed flows from `esp_mqtt_client_config_t` down through esp-tls into mbedTLS; there is no separate TLS configuration surface for the MQTT client.

## Verifying the broker: the certificate bundle

The common failure is embedding a single server certificate in firmware. That pins the leaf, and the entire fleet goes dark the moment the broker's certificate rotates — which for an automatically renewed certificate is a scheduled event, not an accident.

For a broker fronted by a public certificate authority (CA), the **ESP x509 Certificate Bundle** removes that coupling. The bundle compiles a set of Mozilla NSS root certificates into the firmware image and is attached with a single function pointer, `esp_crt_bundle_attach`. Trust anchors are then the roots, so **leaf and intermediate rotation no longer requires a firmware update**.

Menuconfig offers three variants:

- the **full bundle**, the complete set of roots derived from the Mozilla NSS store;
- the **pre-selected common set**, described in its Kconfig help as roughly half the size of the full bundle while still covering around 99 % of sites;
- a **custom bundle** supplied through `CONFIG_MBEDTLS_CUSTOM_CERTIFICATE_BUNDLE_PATH`.

For a private fleet the custom variant is the strongest option: running a private CA and placing **only that root** in the bundle means a node will reject any peer whose chain does not terminate at the fleet's own root. The trust set is one certificate rather than every public CA in the bundle, and the flash footprint shrinks accordingly.

## Mutual TLS: the broker verifies the node

Username and password authentication concentrates risk in a single fleet-wide secret that appears in every flash dump. Per-device client certificates decompose it: each node holds its own key pair and a certificate signed by the fleet CA, the broker requires client authentication, and **revoking one compromised node's certificate leaves every other node unaffected**.

The configuration below combines both directions — bundle-based server verification and a client certificate for mutual authentication.

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
                /* .skip_cert_common_name_check disables hostname
                 * validation; leave it unset outside lab bring-up. */
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

Two ownership rules from the documentation are load-bearing. **The client neither copies nor frees the certificate and key buffers**, so those buffers must remain valid for the whole lifetime of the client handle — a stack-allocated or freed buffer produces a use-after-free at the next reconnect, not at configuration time. And **PEM buffers must be NUL-terminated**: `EMBED_TXTFILES` appends the terminator, `EMBED_FILES` does not, so the same PEM file embedded the second way fails to parse.

Where the part provides one, the Digital Signature (DS) peripheral on the S2, S3, C3 and C6, or an ATECC608 element selected through `use_secure_element`, holds the private key so that it is not stored readable in flash. Absent such hardware, the key sits in the flash image and flash encryption is the remaining mitigation.

## What TLS costs

The flash figure below comes from one S3 gateway build and should be read as order-of-magnitude rather than as a specification. mbedTLS plus the common certificate bundle added roughly **100–150 KB of flash** over the equivalent plaintext build.

Heap is the tighter constraint. A full handshake transiently requires **tens of kilobytes**, dominated by the TLS record buffers. The inbound buffer `CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN` defaults to the TLS maximum record size of 16 KB; the outbound buffer defaults lower, and both are configurable. The Mbed TLS documentation lists shrinking `CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN` and `CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN` first among its memory optimizations. A 4 KB inbound buffer is workable when the broker's certificate chain and its records fit inside it; the limit is a property of the peer, so it must be tested against the actual broker.

```
CONFIG_MBEDTLS_CERTIFICATE_BUNDLE=y
CONFIG_MBEDTLS_CERTIFICATE_BUNDLE_DEFAULT_CMN=y
CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN=4096
CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN=2048
CONFIG_ESP_TLS_CLIENT_SESSION_TICKETS=y
```

## Session resumption on duty-cycled nodes

For a mains-powered node holding one long-lived connection, the handshake is a one-time cost amortized over the session. A battery node that wakes, publishes and returns to deep sleep pays it **on every cycle**, and that cost is radio-on time — the dominant term in the energy budget.

esp-tls supports client-side session resumption. The sequence is: enable `CONFIG_ESP_TLS_CLIENT_SESSION_TICKETS`, retrieve the session after a successful connection with `esp_tls_get_client_session()`, persist it (it survives deep sleep in RTC memory or non-volatile storage), and supply it on the next connection so the full handshake is skipped. The ESP-TLS documentation states that this "significantly reduce[s] the time and resources spent on full TLS handshakes"; the saving is the asymmetric cryptography and the extra round trips of a full handshake, and it accumulates across thousands of wake cycles. No published measurement separates the two cases on this hardware.

Where both peers support TLS 1.3 (`CONFIG_MBEDTLS_SSL_PROTO_TLS1_3`), the initial handshake completes in fewer round trips than TLS 1.2. Broker support must be confirmed before enabling it fleet-wide.

One limitation is worth stating plainly. **Resumption is directly usable with esp-tls and `esp_http_client`, where the session object is under application control. esp-mqtt manages its transport internally**, so a sleep-cycle MQTT node cannot reach the session API through the MQTT client alone; the practical options are to measure whether plain reconnect cost is tolerable, or to send wake-up samples over HTTPS with a saved session and reserve MQTT for always-on gateways.

A useful verification exercise: stand up Mosquitto behind a private CA generated with `openssl`, issue one per-device certificate, rebuild the node with a custom bundle containing only that root, and confirm that the node refuses a connection to any other broker.

## Pitfalls

- **Embedding the broker's leaf certificate instead of a root.** The fleet stops connecting at the next certificate renewal, with a verification failure that looks like a broker outage.
- **Using `EMBED_FILES` for PEM data.** The buffer is not NUL-terminated and esp-tls fails to parse it, even though the file contents are byte-identical to a working `EMBED_TXTFILES` build.
- **Freeing or stack-allocating the certificate and key buffers.** esp-mqtt stores the pointers without copying, so the corruption surfaces on a later reconnect rather than at `esp_mqtt_client_init()`.
- **Setting `.skip_cert_common_name_check` to silence a hostname mismatch.** The connection succeeds and the chain is still validated, but the node will accept any host presenting a chain to a trusted root.
- **Shrinking `CONFIG_MBEDTLS_SSL_IN_CONTENT_LEN` without testing against the real broker.** A peer sending a record larger than the buffer aborts the handshake, and the reported error does not name the buffer as the cause.
- **Retaining a shared username and password alongside client certificates.** The shared secret remains extractable from any flash dump, so the per-device revocation property that mutual TLS provides is not achieved.
- **Enabling TLS 1.3 fleet-wide without confirming broker support.** Nodes fail to negotiate and fall into a reconnect loop that consumes the radio-on time that resumption was meant to save.
