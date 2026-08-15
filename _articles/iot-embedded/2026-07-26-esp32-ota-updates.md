---
title: "OTA updates on the ESP32: patching a fleet that is physically out of reach"
date: 2026-07-26
track: iot-embedded
summary: "A hundred air-quality sensors bolted to poles across town cannot be reached with a USB cable when the firmware needs a fix. The ESP-IDF over-the-air stack end to end — partition layout, esp_https_ota, rollback self-test, and anti-rollback — and what each mechanism costs."
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

**Gist.** Air-quality sensors mounted on streetlight poles and rooftops cannot be re-flashed over a wire when a particulate-matter calibration constant changes or a vulnerability lands in the transport-layer-security library, so the firmware must replace itself over the network. Over-the-air (OTA) update on the ESP32 solves this by writing the new image into a *second* application partition and flipping a bootloader pointer, with a confirmation step that reverts the pointer if the new image never declares itself healthy. The cost is flash: **two full-size application slots plus bookkeeping**, so the usable image budget is roughly half the flash, and every additional guarantee — rollback, anti-rollback, signing — adds either a boot-time check or a one-way electrical fuse (eFuse) burn. Described against the ESP-IDF stable documentation for the ESP32.

## The partition scheme: application slots and one pointer

OTA on the ESP32 never overwrites the running application in place. The running image writes the incoming one into a *different* application partition and then changes which partition the bootloader selects. An OTA-capable board therefore needs at least two application partitions plus a small bookkeeping partition:

| Name | Type | Subtype | Typical size | Purpose |
|---|---|---|---|---|
| `nvs` | data | nvs | 0x6000 | Wi-Fi credentials, calibration data |
| `otadata` | data | ota | 0x2000 | Fixed size — records which application slot is active |
| `factory` | app | factory | 1M | Original image; optional if devices ship directly to ota_0/ota_1 |
| `ota_0` | app | ota_0 | 1M | First OTA slot |
| `ota_1` | app | ota_1 | 1M | Second OTA slot |

A minimal `partitions.csv` for a two-slot layout:

```csv
# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x6000,
otadata,  data, ota,     0xf000,  0x2000,
factory,  app,  factory, 0x10000, 1M,
ota_0,    app,  ota_0,   ,        1M,
ota_1,    app,  ota_1,   ,        1M,
```

`otadata` is the piece whose absence is hardest to diagnose, because a device without it boots normally and silently ignores every update. It **holds the data for OTA updates**: which application slot the bootloader should boot next, and whether that slot has been confirmed good. **When `otadata` is blank — first boot, or a factory-flashed device — the bootloader falls back to the `factory` partition, or to `ota_0` when the table declares no factory partition.** After the first successful `esp_ota_set_boot_partition()`, `otadata` governs selection and the factory slot serves only as a recovery image.

The partition table is fixed at flash time, so **sizing is a decision that cannot be revised without an OTA whose only purpose is to migrate the layout**. A 1M slot is tight once TLS, a JSON parser and a sensor driver stack are linked into one binary; `idf.py size` reports the actual figure before the layout is locked.

## Pulling the update: esp_https_ota

The `esp_https_ota` component encapsulates the HTTPS GET, the image-header validation and the flash-write loop. The one-call form covers the common case:

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

`cert_pem` pins the update server's certificate, which is what prevents a device on public cellular or a third party's Wi-Fi from accepting any chain a local resolver or proxy can produce.

The single call is opaque: it returns only when the transfer has finished or failed. Where progress reporting or mid-transfer arbitration is required, the granular application programming interface (API) exposes the same state machine step by step:

1. `esp_https_ota_begin()` opens the connection and returns a handle.
2. A loop calls `esp_https_ota_perform()`, which returns `ESP_ERR_HTTPS_OTA_IN_PROGRESS` while more data remains; **any other return value terminates the loop**.
3. `esp_https_ota_finish()` validates the image and switches the boot partition.
4. The application issues its own `esp_restart()`.

`esp_https_ota_abort()` releases the handle without committing, which is the exit for a diagnostic decision taken mid-download — a battery-powered node concluding that it lacks the charge to finish, for example.

Scheduled polling of a version endpoint requires no inbound reachability and no per-device push registration, which is why it is the usual trigger for a fleet behind carrier network-address translation.

## Rollback: the pending-verify state

`esp_https_ota_finish()` changes the boot pointer; it does not establish that the new firmware runs. `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` adds the missing step. With it set, a freshly flashed application boots into the **`ESP_OTA_IMG_PENDING_VERIFY` state: the bootloader has provisionally selected the slot but will revert to the previous one on the next reset unless the application affirmatively confirms itself.**

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

The invariant that makes this safe is that **confirmation requires executing code in the new image**. A failure mode that prevents reaching `esp_ota_mark_app_valid_cancel_rollback()` — a panic, a watchdog reset, a hang before the check — leaves the slot unconfirmed, and the bootloader reverts on the next boot without any external intervention. That covers the entire class of images that do not survive their own startup.

It does not cover images that boot, confirm, and are wrong afterwards. `self_test_passed()` therefore has to assert something the device can check at startup: the particulate sensor answers over its universal asynchronous receiver-transmitter (UART) link, the connection to the message-queue ingest broker succeeds, the real-time clock retained its value. Anything not asserted before confirmation is outside the rollback guarantee.

## Anti-rollback: refusing older security versions

Application rollback addresses a broken new image. Anti-rollback addresses an old one being replayed — a previously valid firmware file with a since-patched vulnerability, served back to a device that already installed the fix. `CONFIG_BOOTLOADER_APP_ANTI_ROLLBACK` burns a monotonic security version into eFuse and compares it against the `secure_version` field in each application's image header, set by `CONFIG_BOOTLOADER_APP_SECURE_VERSION`. **The bootloader refuses to boot, and OTA refuses to install, any image whose secure version is lower than the value burned in eFuse.**

The cost is irreversibility and a finite budget. `CONFIG_BOOTLOADER_APP_SEC_VER_SIZE_EFUSE_FIELD` sets how many eFuse bits the field occupies — on the ESP32 up to 32 — and the version is encoded as the *number of bits burned*, so **a field of N bits permits N increments in total, and none of them can be undone**. This is why the version tracks vulnerability fixes rather than release numbers. `esp_efuse_check_secure_version()` allows a device to reject a downgrade before downloading the image, which on a connection billed by the megabyte is the difference between a rejected update and a rejected update that was paid for.

## Signing: what TLS alone does not establish

A pinned certificate authenticates the *server*, not the *binary*. Secure Boot v2 signs the second-stage bootloader and the application image — on the ESP32 with an RSA-3072 key under RSA-PSS; the read-only-memory (ROM) bootloader verifies the second-stage bootloader and the second-stage bootloader verifies the application before executing it. With Secure Boot enabled, the OTA path also verifies the incoming image's signature before committing it to a boot slot.

The four mechanisms cover four distinct attacks and none substitutes for another: **TLS with a pinned certificate protects the transfer, the image signature establishes provenance, anti-rollback blocks replay of an old signed image, and application rollback catches build defects**. Without signing, a compromised distribution host or a hijacked name resolution reaches every device that polls that URL.

**Try next:** route the `esp_https_ota` event loop (`ESP_HTTPS_OTA_START`, `ESP_HTTPS_OTA_WRITE_FLASH`, `ESP_HTTPS_OTA_FINISH`) to a status topic so OTA progress and rollback events reach the ingest pipeline rather than failing silently on an unreachable mount.

## Pitfalls

- **Omitting the `otadata` partition.** The device boots and runs normally, and OTA appears to succeed, but the bootloader has nowhere to record the slot selection and keeps booting the old image.
- **Sizing application slots without measuring.** A binary that outgrows its 1M slot fails at link or flash time after devices are already deployed, and resizing the table requires an OTA whose only purpose is migration.
- **Enabling rollback without calling the confirmation function.** Every update reverts on the second reboot, presenting as a fleet that installs firmware successfully and then mysteriously returns to the previous version.
- **A `self_test_passed()` that asserts nothing.** A trivially true self-test confirms the slot immediately, so the rollback machinery is enabled and configured but guarantees nothing beyond "app_main was reached".
- **Bumping `CONFIG_BOOTLOADER_APP_SECURE_VERSION` on every release.** Each raise burns one more eFuse bit out of a field of at most 32 on the ESP32, and no bit can be returned; exhausting the field removes the ability to raise the version again, and every already-deployed image below the burned value becomes unbootable.
- **Treating a pinned TLS certificate as image authentication.** It proves which host served the bytes, not who built them, so any compromise of the build or distribution pipeline still delivers executable code.
- **Ignoring the `esp_https_ota_perform()` return value other than for `ESP_ERR_HTTPS_OTA_IN_PROGRESS`.** An error return exits the loop identically to a completed transfer, so a truncated download proceeds to `esp_https_ota_finish()` unless the value is inspected.
