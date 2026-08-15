---
title: "BLE provisioning of Wi-Fi credentials on a headless ESP32"
date: 2026-07-31
track: iot-embedded
summary: "Hardcoding a service set identifier (SSID) and passphrase into a firmware image does not survive a real deployment. The ESP-IDF wifi_provisioning manager advertises a Bluetooth Low Energy service, receives credentials over an encrypted session, and stores them in non-volatile storage. This article covers the BLE scheme, the Security1-versus-Security2 trade-off, and the initialisation sequence."
reading_time: 5
tags: [esp32, wifi, provisioning, ble, esp-idf, security]
sources:
  - title: "Wi-Fi Provisioning — ESP-IDF Programming Guide (v5.1)"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.1/esp32/api-reference/provisioning/wifi_provisioning.html"
  - title: "Unified Provisioning — ESP-IDF Programming Guide (v5.1)"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.1/esp32/api-reference/provisioning/provisioning.html"
  - title: "ESP BLE Provisioning app (Google Play)"
    url: "https://play.google.com/store/apps/details?id=com.espressif.provble"
  - title: "ESP-IDF tutorial series: Wi-Fi Provisioning (Espressif Developer Portal)"
    url: "https://developer.espressif.com/blog/2026/05/simple-provisioning/"
---

**Gist.** A headless ESP32 sensor node has neither keyboard nor display, yet it requires a service set identifier (SSID) and passphrase before it can join a network; embedding them in the firmware image leaks them to anyone who dumps flash and binds one binary to one site. Provisioning replaces the constant with a protocol: the device boots into a temporary configuration state, advertises a Bluetooth Low Energy (BLE) service, receives credentials over an encrypted session, and writes them to non-volatile storage (NVS). The cost is a second radio stack resident in flash and RAM during the provisioning window, plus a secret that must be distributed to the provisioner out of band.

## The manager and its transport schemes

The ESP-IDF component is `wifi_provisioning`, driven through the `wifi_prov_mgr_*` API. It is transport-agnostic: the application selects a **scheme**, and the manager runs the protocol above it. Two schemes are practical — `wifi_prov_scheme_ble`, in which the device advertises a BLE Generic Attribute Profile (GATT) service, and `wifi_prov_scheme_softap`, in which the device hosts a temporary soft access point and provisioning runs over HTTP.

The trade-off is concrete. With SoftAP the provisioning phone must **leave its own network** to associate with the throwaway access point, an access point that carries no route to the internet; BLE requires no association change, at the cost of the Bluetooth stack's flash and RAM footprint. On parts without Bluetooth — the ESP32-S2 — **SoftAP is the only available scheme**.

Beneath either transport the manager carries **protocomm** endpoints: a session-establishment endpoint that runs the handshake, a `wifi_config` endpoint that receives the SSID and passphrase, a `wifi_scan` endpoint, and optional application-defined endpoints. The application does not invoke these directly; the manager registers and dispatches them. Because both schemes carry the same endpoints, **the message formats exchanged with the provisioner are identical across BLE and SoftAP** — only the transport framing below protocomm differs.

## Security1 and Security2

Credentials cross the air, so the session is encrypted. The choice concerns how the two ends authenticate to each other.

- **`WIFI_PROV_SECURITY_1`** performs an X25519 (Curve25519) key exchange authenticated by a shared **proof-of-possession (PoP)** string, then encrypts the session with AES in counter mode (AES-CTR). The PoP is a short secret printed on a label or encoded in a QR code. **Without the PoP an attacker within BLE range cannot complete the handshake**, and therefore cannot reach the `wifi_config` endpoint.
- **`WIFI_PROV_SECURITY_2`** performs **SRP6a**, an augmented password-authenticated key exchange, then encrypts with AES in Galois/Counter Mode (AES-GCM). The load-bearing property is that **the device stores only an SRP6a verifier, never the password itself**, so an attacker who dumps flash does not recover the credential a legitimate provisioner presents. Security2 therefore requires that a per-device salt and verifier be generated and flashed at manufacture.
- **`WIFI_PROV_SECURITY_0`** disables encryption entirely and is a bench-testing configuration.

The practical split follows the manufacturing capability rather than the threat model alone: Security1 where every unit is flashed from one image and the PoP is a build-time constant, Security2 where the line can write a **unique salt and verifier per unit**. A PoP shared across an entire fleet degrades Security1 to a single compromise: extracting it from one unit unlocks every other unit in range.

## Initialisation and the provisioned check

The sequence is: initialise NVS, the network interface and the default event loop; initialise the manager with the BLE scheme; query whether credentials are already stored; and start provisioning only if they are not. The `WIFI_PROV_SCHEME_BLE_EVENT_HANDLER_FREE_BTDM` flag instructs the manager to release the Bluetooth Classic (BT/BR/EDR) controller memory that BLE-only operation does not use.

```c
#include "wifi_provisioning/manager.h"
#include "wifi_provisioning/scheme_ble.h"

void start_provisioning(void) {
    wifi_prov_mgr_config_t config = {
        .scheme = wifi_prov_scheme_ble,
        .scheme_event_handler = WIFI_PROV_SCHEME_BLE_EVENT_HANDLER_FREE_BTDM,
    };
    ESP_ERROR_CHECK(wifi_prov_mgr_init(config));

    bool provisioned = false;
    ESP_ERROR_CHECK(wifi_prov_mgr_is_provisioned(&provisioned));

    if (!provisioned) {
        // Security1: X25519 authenticated by PoP, then AES-CTR.
        wifi_prov_security_t security = WIFI_PROV_SECURITY_1;
        const char *pop = "abcd1234";              // secret the provisioner must present
        const char *service_name = "PROV_A1B2C3";  // BLE advertised name
        const char *service_key  = NULL;           // unused by the BLE scheme

        ESP_ERROR_CHECK(wifi_prov_mgr_start_provisioning(
            security, (const void *)pop, service_name, service_key));
        // The manager runs in its own task; deinit here would tear it down
        // mid-handshake. Deinit after WIFI_PROV_CRED_SUCCESS.
    } else {
        wifi_prov_mgr_deinit();                    // credentials in NVS
        // esp_wifi_set_mode(WIFI_MODE_STA); esp_wifi_start();
    }
}
```

`wifi_prov_mgr_is_provisioned` reads the stored Wi-Fi station configuration, so the check is a **flash read rather than a connection attempt**: a provisioned device that cannot currently reach its access point still reports `true` and does not re-advertise. Recovery from a changed network therefore requires an explicit reset path, typically `wifi_prov_mgr_reset_provisioning`, bound to a physical input.

`service_name` is the identifier presented in the phone application's device list; Espressif's examples derive it from the device MAC address so that units in the same room remain distinguishable. The application registers a handler on the `WIFI_PROV_EVENT` base and observes the state machine: **`WIFI_PROV_CRED_RECV`** when the `wifi_config` endpoint delivers an SSID and passphrase, then either **`WIFI_PROV_CRED_SUCCESS`** once the device associates with those credentials or **`WIFI_PROV_CRED_FAIL`** if it does not. Only the success transition justifies `wifi_prov_mgr_deinit`; on failure the manager remains available so the provisioner can retry with corrected input.

## The provisioning application

Writing a phone application is unnecessary: Espressif publishes **ESP BLE Provisioning** and **ESP SoftAP Provisioning** for Android and iOS. The BLE application scans for the advertised `service_name`, prompts for the PoP or reads a QR code encoding name, PoP and transport, completes the Security1 or Security2 handshake, presents the access points the device itself scanned through the `wifi_scan` endpoint, and transmits the selected SSID and passphrase over the established session. The `esp_prov` Python helper shipped with ESP-IDF performs the same exchange from a host machine and is the practical tool for reproducing a failed handshake without a phone.

## Pitfalls

- Calling `wifi_prov_mgr_deinit` immediately after `wifi_prov_mgr_start_provisioning` aborts provisioning: the manager runs in its own task, and the start call returns before any handshake occurs.
- Treating `WIFI_PROV_CRED_RECV` as completion provisions unusable credentials, because that event fires when the passphrase arrives, not when association with it succeeds; only `WIFI_PROV_CRED_SUCCESS` confirms the credentials work.
- A device whose access point changed SSID never re-enters provisioning, because `wifi_prov_mgr_is_provisioned` reports the stored configuration rather than reachability; without a reset input the unit requires reflashing.
- One PoP string reused across a fleet makes every unit provisionable by anyone who extracts the string from a single unit, since Security1 derives its authentication solely from that shared value.
- Selecting the BLE scheme on an ESP32-S2 cannot work, as the part has no Bluetooth controller; SoftAP is the only scheme available there.
- Leaving `WIFI_PROV_SECURITY_0` in a shipped build exposes the `wifi_config` endpoint unencrypted to anyone in BLE range, and the symptom is silent — provisioning succeeds normally.
