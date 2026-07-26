---
title: "OTA updates on the ESP32: patching a fleet you'll never touch again"
date: 2026-07-26
track: iot-embedded
summary: "A hundred air-quality sensors bolted to poles across town don't get a USB cable when the firmware needs a fix. Here's the ESP-IDF OTA stack end to end — partition layout, esp_https_ota, rollback self-test, and anti-rollback — so a bad push doesn't mean a truck roll."
reading_time: 6
tags: [esp32, esp-idf, ota, embedded, security, rollback, air-quality]
sources:
  - title: "ESP HTTPS OTA — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/esp_https_ota.html"
  - title: "Over The Air Updates (OTA) — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/ota.html"
  - title: "Partition Tables — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/partition-tables.html"
  - title: "Staying Ahead with ESP32 Security Updates — Espressif Developer Portal"
    url: "https://developer.espressif.com/blog/2026/03/esp32-security-updates/"
  - title: "esp-idf/examples/system/ota — GitHub"
    url: "https://github.com/espressif/esp-idf/blob/master/examples/system/ota/README.md"
---

The air-quality sensors in this fleet live on streetlight poles and rooftop mounts. Nobody is climbing back up there to plug in a USB cable because a PM2.5 calibration constant needs a tweak, or because a CVE landed in mbedTLS. If the firmware can't update itself, safely, over the network the device shipped with, the fleet is frozen the day it goes into the field. This is the ESP-IDF OTA stack, end to end, with the parts that actually bite: partition layout, `esp_https_ota`, rollback, and anti-rollback. Tested against ESP-IDF stable (v6.0.2).

## The partition scheme: three app slots, one pointer

OTA on ESP32 doesn't overwrite the running app in place — it writes the new image to a *different* app partition and then flips a pointer. That means every OTA-capable board needs at least two app partitions plus a small bookkeeping partition:

| Name | Type | Subtype | Typical size | Purpose |
|---|---|---|---|---|
| `nvs` | data | nvs | 0x6000 | Wi-Fi creds, calibration data |
| `otadata` | data | ota | 0x2000 | Fixed size — records which app slot is active |
| `factory` | app | factory | 1M | Original image, optional if you ship straight to ota_0/ota_1 |
| `ota_0` | app | ota_0 | 1M | First OTA slot |
| `ota_1` | app | ota_1 | 1M | Second OTA slot |

A minimal `partitions.csv` for a two-slot OTA layout looks like this:

```csv
# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x6000,
otadata,  data, ota,     0xf000,  0x2000,
factory,  app,  factory, 0x10000, 1M,
ota_0,    app,  ota_0,   ,        1M,
ota_1,    app,  ota_1,   ,        1M,
```

The `otadata` partition is the one people forget and then can't debug. It "holds the data for OTA updates" — specifically which app slot the bootloader should boot next and whether that slot is confirmed good. If `otadata` is blank (first boot, factory-flashed device), the bootloader defaults to `factory`. Once you `esp_ota_set_boot_partition()` for the first time, `otadata` takes over and the factory slot becomes just a recovery fallback. For a fleet, size the app partitions with headroom — 1M is tight once TLS, a JSON parser, and a sensor driver stack are all linked in; check `idf.py size` before locking the layout, because resizing partitions on devices already in the field means another OTA just to migrate.

## Pulling the update: esp_https_ota

The `esp_https_ota` component wraps the whole HTTP(S) GET, image-header validation, and flash-write loop. For most devices the one-call form is enough:

```c
#include "esp_https_ota.h"
#include "esp_ota_ops.h"

static void ota_task(void *pv)
{
    esp_http_client_config_t http_cfg = {
        .url = "https://updates.example.com/sensor-fw/latest.bin",
        .cert_pem = (char *)server_cert_pem_start,
        .keep_alive_enable = true,
        .timeout_ms = 30000,
    };
    esp_https_ota_config_t ota_cfg = {
        .http_config = &http_cfg,
    };

    esp_err_t err = esp_https_ota(&ota_cfg);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "OTA applied, rebooting");
        esp_restart();
    } else {
        ESP_LOGE(TAG, "OTA failed: %s", esp_err_to_name(err));
    }
    vTaskDelete(NULL);
}
```

`cert_pem` pins the server's TLS certificate — non-negotiable when the request travels over public cellular or a customer's Wi-Fi. If you need progress reporting (useful on a sensor with a status LED or a low-bandwidth LTE-M link) or want to pause OTA when a measurement cycle is in progress, drop down to the granular API instead: `esp_https_ota_begin()` to open the connection, a loop calling `esp_https_ota_perform()` until it returns anything other than `ESP_ERR_HTTPS_OTA_IN_PROGRESS`, then `esp_https_ota_finish()` to validate the image and switch the boot partition, followed by your own `esp_restart()`. `esp_https_ota_abort()` exists for the case where a diagnostic check mid-download says "not now" — a battery-powered node deciding it doesn't have the charge to finish, for instance.

Trigger this from a scheduled check-in rather than a push channel you have to keep provisioned — a fleet of sensors polling a version endpoint hourly and pulling only on a version bump scales a lot better than a fan-out you have to manage.

## Rollback: surviving your own bad build

`esp_https_ota_finish()` switches the boot pointer, but it does not mean the new firmware *works*. This is where `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` earns its keep. With it set, a freshly flashed app boots into the `ESP_OTA_IMG_PENDING_VERIFY` state — the bootloader has provisionally selected it but will revert to the previous slot on the next reset unless the app affirmatively confirms itself:

```c
#include "esp_ota_ops.h"

void app_main(void)
{
    // ... bring up sensors, Wi-Fi, MQTT ...

    if (self_test_passed()) {
        esp_ota_mark_app_valid_cancel_rollback();
    } else {
        // Reboots immediately into the previous known-good image
        esp_ota_mark_app_invalid_rollback_and_reboot();
    }
}
```

`self_test_passed()` should mean something concrete for the device — the PM sensor answers over UART, the MQTT connection to the ingest broker succeeds, the RTC hasn't reset. If the new image panics or watchdog-resets before calling `esp_ota_mark_app_valid_cancel_rollback()`, the bootloader treats the pending-verify slot as unconfirmed and rolls back automatically on the next boot — no field visit required. This is the single feature that turns "we bricked forty sensors" into "forty sensors quietly reverted and logged an error." Don't skip it to save a menuconfig option.

## Anti-rollback: closing the downgrade hole

App rollback protects you from a broken new image. Anti-rollback protects you from a *malicious* old one — someone replaying a firmware file with a known vulnerability back onto a device that already patched it. `CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK` burns a monotonic security version into eFuse and compares it against the `secure_version` field embedded in each app's image header (set via `CONFIG_BOOTLOADER_APP_SECURE_VERSION`). The bootloader refuses to boot — and OTA refuses to install — any image whose secure version is lower than what's already burned. Bump `CONFIG_BOOTLOADER_APP_SECURE_VERSION` on releases that fix a real vulnerability, not on every point release, since each bump is a one-way eFuse burn with a finite bit budget (`CONFIG_BOOTLOADER_APP_SEC_VER_SIZE_EFUSE_FIELD` caps it at 32 bits). `esp_efuse_check_secure_version()` lets you reject a downgrade before spending bandwidth downloading the whole image, which matters more than it sounds like on a cellular-connected sensor billed by the megabyte.

## Signing it: secure boot ties the bow

None of the above stops someone from serving a *different* binary at your update URL if they can intercept or spoof the connection. Secure Boot v2 signs the bootloader and app image with an ECDSA or RSA key; the ROM bootloader and second-stage bootloader verify the signature chain before executing anything, and `esp_https_ota` verifies the incoming image's signature before it's committed to a boot slot. Combined with flash encryption, this closes the loop: TLS with a pinned cert protects the download in transit, the image signature proves it came from your build pipeline, anti-rollback stops replay of an old signed-but-vulnerable image, and app rollback catches your own mistakes. For a fleet you can't physically reach, treat all four as one feature, not four checkboxes — skip signing and a compromised CDN or DNS hijack can push arbitrary code to every pole-mounted sensor in the city at once.

**Try next:** wire `esp_https_ota`'s event loop (`ESP_HTTPS_OTA_START` / `ESP_HTTPS_OTA_WRITE_FLASH` / `ESP_HTTPS_OTA_FINISH`) into your MQTT status topic so the fleet reports OTA progress and rollback events back to the ingest pipeline instead of failing silently on a pole you'll never climb.
