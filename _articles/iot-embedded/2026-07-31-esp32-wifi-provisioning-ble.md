---
title: "Getting Wi-Fi creds onto a headless ESP32 with BLE provisioning"
date: 2026-07-31
track: iot-embedded
summary: "Hardcoding an SSID and password into firmware doesn't survive contact with a real deployment. ESP-IDF's wifi_provisioning manager solves it: the device advertises over BLE, a phone app hands it credentials through an encrypted channel, and the manager stores them in NVS. Here's the BLE scheme, the Security1-vs-Security2 tradeoff, and the init code."
reading_time: 6
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

A headless ESP32 sensor node has no keyboard and no screen, yet it needs your Wi-Fi SSID and password before it can do anything. Baking them into the firmware image is the tempting shortcut, and it fails in every direction that matters: the credentials leak to anyone who dumps flash, they can't change when you move the device to a new network, and you can't ship one binary to more than one site. **Provisioning** is the alternative — the device comes up in a temporary "please configure me" state, a phone hands it credentials over an encrypted transport, it stores them in NVS, and it reboots as a normal station. ESP-IDF ships a manager that does all of this; over BLE it needs surprisingly little code.

## The provisioning manager and the BLE scheme

The component is `wifi_provisioning`, driven through `wifi_prov_mgr_*` calls. It's transport-agnostic: you pick a **scheme** and the manager handles the protocol on top. The two real choices are `wifi_prov_scheme_ble` (the device advertises a BLE GATT service) and `wifi_prov_scheme_softap` (the device hosts a temporary SoftAP and you provision over HTTP). BLE is usually the better default on an ESP32 — the phone never has to leave your home Wi-Fi to join a throwaway access point, and reconnection is smoother — at the cost of the BLE stack's flash and RAM footprint. On chips with no Bluetooth (the classic ESP32-S2), SoftAP is your only option.

Under the hood the transport carries **protocomm** endpoints: a session-establishment handshake, the `wifi_config` endpoint that receives the SSID/passphrase, and optional custom endpoints for your own data. You don't call those directly — the manager wires them up.

## Security1 vs Security2

Credentials cross the air, so the session is encrypted, and you choose how the two ends authenticate:

- **`WIFI_PROV_SECURITY_1`** — an X25519 (Curve25519) key exchange, authenticated by a shared **proof-of-possession (PoP)** string, then AES-CTR for the session. The PoP is a short secret you print on a label or ship in a QR code; without it a stranger in BLE range can't complete the handshake. Simple and adequate for most consumer gear.
- **`WIFI_PROV_SECURITY_2`** — **SRP6a** (an augmented password-authenticated key exchange) followed by AES-GCM. Crucially, the device stores only an SRP6a *verifier*, not the password, so dumping the device's flash doesn't reveal the credential a legitimate provisioner uses. This is the stronger choice when the salt/verifier can be provisioned per-device at manufacture.
- **`WIFI_PROV_SECURITY_0`** exists — no encryption — and is for bench testing only.

Rule of thumb: **Security1 with a per-device PoP** for a hobby or small fleet; **Security2** when you can generate a unique salt/verifier per unit on the line.

## Initializing and starting it

The skeleton is: init NVS and the netif/event loop, init the manager with the BLE scheme, check whether credentials already exist, and only start provisioning if they don't. The `WIFI_PROV_SCHEME_BLE_EVENT_HANDLER_FREE_BTDM` flag tells the manager to release the classic-Bluetooth memory it doesn't need once provisioning ends.

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
        // Security1: X25519 + proof-of-possession, then AES-CTR.
        wifi_prov_security_t security = WIFI_PROV_SECURITY_1;
        const char *pop = "abcd1234";              // per-device secret
        const char *service_name = "PROV_A1B2C3";  // BLE advertised name
        const char *service_key  = NULL;           // unused for BLE

        ESP_ERROR_CHECK(wifi_prov_mgr_start_provisioning(
            security, (const void *)pop, service_name, service_key));
        // Do NOT deinit here — the manager runs in its own task.
        // Wait on WIFI_PROV_EVENT_CRED_SUCCESS, then wifi_prov_mgr_deinit().
    } else {
        wifi_prov_mgr_deinit();                     // creds in NVS: just connect
        // esp_wifi_set_mode(WIFI_MODE_STA); esp_wifi_start();
    }
}
```

The `service_name` is what shows up in the phone app's device list — Espressif's examples derive it from the MAC so each unit is distinguishable. Register an event handler for the `WIFI_PROV_EVENT` base to react to `WIFI_PROV_CRED_RECV`, `WIFI_PROV_CRED_SUCCESS`, and `WIFI_PROV_CRED_FAIL`; on success you tear down the manager and the stored credentials drive the normal STA connect on every subsequent boot.

## The phone side

You don't have to write an app. Espressif ships **ESP BLE Provisioning** and **ESP SoftAP Provisioning** for Android and iOS. The BLE app scans for the advertised `service_name`, prompts for the PoP (or scans a QR code that encodes name + PoP + transport), completes the Security1/Security2 handshake, lists nearby Wi-Fi networks the device scanned, and sends your chosen SSID and password over the encrypted session. Because the whole protocol is protocomm underneath, the same reference app works for both BLE and SoftAP schemes — only the transport differs.

**Try next:** Flash the ESP-IDF `wifi_prov_mgr` example, generate a per-device PoP QR code with the `esp_prov` Python helper, and provision it from the phone app — then reboot with credentials already in NVS and confirm `wifi_prov_mgr_is_provisioned` short-circuits straight to a station connect without re-advertising.
