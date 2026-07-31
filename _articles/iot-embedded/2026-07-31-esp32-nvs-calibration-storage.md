---
title: "Persisting Sensor Calibration on the ESP32 with NVS"
date: 2026-07-31
track: iot-embedded
summary: "Your ADC offset or air-quality baseline shouldn't reset every power cycle. NVS is the ESP32's wear-leveled key-value store in flash — here's how to read a float calibration value with a first-boot default, write it back, and why you must commit. Plus the float gotcha in ESP-IDF 5.x."
reading_time: 5
tags: [esp32, esp-idf, nvs, flash, calibration, arduino]
sources:
  - title: "Non-Volatile Storage Library — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/storage/nvs_flash.html"
  - title: "NVS API (ESP-IDF v5.1 — float documented as non-native)"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.1/esp32/api-reference/storage/nvs_flash.html"
  - title: "NVS Encryption — ESP-IDF Programming Guide"
    url: "https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/storage/nvs_encryption.html"
  - title: "Arduino-ESP32 Preferences library reference"
    url: "https://docs.espressif.com/projects/arduino-esp32/en/latest/tutorials/preferences.html"
  - title: "ESP32 Save Data Permanently using Preferences Library — Random Nerd Tutorials"
    url: "https://randomnerdtutorials.com/esp32-save-data-permanently-preferences/"
---

You calibrate an air-quality node once — measure the ADC offset against a reference, note the sensor's clean-air baseline — and then the board reboots and forgets all of it. Hardcoding the value defeats the point of calibrating per-unit. What you want is a tiny slice of flash that survives power cycles and firmware updates, and on the ESP32 that's **NVS**, the Non-Volatile Storage library.

## What NVS actually is

NVS is a **key-value store in a flash partition** (the `nvs` partition in your partition table) with **wear leveling built in** — writes append to free space rather than rewriting a fixed location, so you're not burning the same flash cells on every save. Keys are grouped into **namespaces** (opened with `nvs_open`), so independent modules can reuse key names without colliding. A few constraints to keep in your head: key and namespace names are capped at **15 characters**, strings at 4000 bytes, and blobs are large but bounded by the partition. Writes are *staged* until you call **`nvs_commit()`** — skip it and your value can vanish on the next reset.

The one wrinkle: in **ESP-IDF 5.x the C API has no native `float`**. Types are the integer widths, zero-terminated strings, and variable-length blobs; the docs literally say float and double "might be added later." (They did, in ESP-IDF 6.0's `nvs_set_float`/`nvs_get_float` — but for 5.x you store a float as a 4-byte blob, or as a fixed-point integer.) On the Arduino side, the `Preferences` wrapper *does* give you `putFloat`/`getFloat` because it serializes the float for you onto the same NVS partition.

## Reading and writing a calibration offset (ESP-IDF 5.x)

The pattern is: bring up NVS, open a namespace, read the offset with a **first-boot default** when the key is missing, and commit after every write.

```c
#include "nvs_flash.h"
#include "nvs.h"

#define CAL_NS      "sensor_cfg"   // <= 15 chars
#define CAL_KEY     "adc_offset"   // <= 15 chars
#define DEFAULT_OFFSET 0.0f

esp_err_t app_nvs_init(void) {
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());     // stale/new-format partition
        err = nvs_flash_init();
    }
    return err;
}

float cal_load_offset(void) {
    nvs_handle_t h;
    float offset = DEFAULT_OFFSET;
    esp_err_t err = nvs_open(CAL_NS, NVS_READONLY, &h);
    if (err == ESP_ERR_NVS_NOT_FOUND) return DEFAULT_OFFSET;  // namespace absent -> first boot
    ESP_ERROR_CHECK(err);

    size_t len = sizeof(offset);
    err = nvs_get_blob(h, CAL_KEY, &offset, &len);            // float stored as 4-byte blob
    if (err != ESP_OK || len != sizeof(offset)) offset = DEFAULT_OFFSET;
    nvs_close(h);
    return offset;
}

esp_err_t cal_store_offset(float offset) {
    nvs_handle_t h;
    esp_err_t err = nvs_open(CAL_NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return err;
    err = nvs_set_blob(h, CAL_KEY, &offset, sizeof(offset));  // staged
    if (err == ESP_OK) err = nvs_commit(h);                   // flushed to flash
    nvs_close(h);
    return err;
}
```

The `ESP_ERR_NVS_NOT_FOUND` branch is the whole trick for robust first-boot behavior: a missing key (or missing namespace) is not an error, it's your cue to substitute the compiled-in default and carry on. That's how a freshly flashed board runs with sane values before anyone has calibrated it.

## The Arduino shortcut

If you're on Arduino-ESP32, `Preferences` collapses this to a few lines against the same partition, with a native float and a built-in default argument:

```cpp
#include <Preferences.h>
Preferences prefs;

float loadOffset() {
  prefs.begin("sensor_cfg", true);                 // read-only
  float off = prefs.getFloat("adc_offset", 0.0f);  // default on first boot
  prefs.end();
  return off;
}
void storeOffset(float off) {
  prefs.begin("sensor_cfg", false);                // read-write
  prefs.putFloat("adc_offset", off);               // commits internally
  prefs.end();
}
```

One more thing worth knowing before you store anything sensitive: NVS can be **encrypted**, backed by flash encryption using XTS-AES, with the keys living in a dedicated `nvs_keys` partition. When flash encryption is on, NVS encryption is on by default and your `nvs_get_*`/`nvs_set_*` calls keep working transparently — so a stolen device doesn't hand over your Wi-Fi password in plaintext.

**Try next:** Add a serial command to your node that captures the current raw ADC reading against a known reference, computes the offset, and calls `cal_store_offset`. Power-cycle and confirm `cal_load_offset` returns it. Then wipe with `idf.py erase-flash` (or `nvs_flash_erase`) and verify the node falls back to `DEFAULT_OFFSET` cleanly instead of reading garbage.
