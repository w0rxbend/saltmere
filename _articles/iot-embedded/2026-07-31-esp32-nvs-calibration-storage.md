---
title: "Persisting Sensor Calibration on the ESP32 with NVS"
date: 2026-07-31
track: iot-embedded
summary: "An ADC offset or air-quality baseline must survive power cycles. NVS is the ESP32's wear-levelled key-value store in flash: reading a float calibration value with a first-boot default, writing it back, the commit requirement, and the absence of a native float type in ESP-IDF 5.x."
reading_time: 6
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

**Gist.** Per-unit calibration constants — an analogue-to-digital converter (ADC) offset measured against a reference, a gas sensor's clean-air baseline — are determined once and must outlive every reset and firmware update, which rules out compiling them into the image. The ESP32's Non-Volatile Storage (NVS) library provides a key-value store inside a dedicated flash partition, with wear levelling and named scopes, reached through `nvs_get_*` and `nvs_set_*`. The cost is a stricter contract than a variable in random-access memory (RAM): names are length-limited, writes are staged until an explicit commit, the partition can refuse a write when it has no free pages, and in ESP-IDF 5.x there is no native floating-point type, so a calibration float travels as a blob.

## The storage model

NVS occupies a **partition of type `data`, subtype `nvs`**, declared in the project's partition table. Within it, entries are addressed by a pair: a **namespace** opened with `nvs_open`, and a **key** within that namespace. The namespace makes key names local to a module, so a sensor-calibration component and a networking component can both own a key called `offset` without collision.

Writes do not rewrite the previous location. NVS appends the new entry into free space within a page and marks the old entry erased; whole pages are reclaimed by NVS's own housekeeping rather than synchronously by the caller. The consequence for the caller is that **a write consumes space even when it overwrites an existing key**.

Three limits are load-bearing. **Key names and namespace names are capped at 15 characters** — a longer literal is not a compile-time error in ordinary C, so it surfaces at runtime as an error code from `nvs_set_*`. **Strings are capped at 4000 bytes.** Blobs have both a fixed upper bound and a bound derived from the size of the NVS partition, whichever is smaller, so blob capacity is partly a property of the partition table entry.

The fourth, and the one that produces the most confusing bug reports: **`nvs_set_*` stages a value; `nvs_commit()` flushes it.** Between the two, the new value is visible to reads through the same handle but is not guaranteed to be on flash. A reset in that window — a watchdog, a brownout, a user pressing the button — leaves the previous value in place, with no error reported anywhere.

## The float gap in ESP-IDF 5.x

In **ESP-IDF 5.x the C API has no native `float`**. The supported types are the fixed-width integers, zero-terminated strings, and variable-length blobs; the v5.1 documentation states that support for float and double "might be added later". Until such a type exists, the two workable encodings are:

- **A 4-byte blob** holding the object representation of the `float`. The value round-trips correctly because it is written and read by the same architecture and the same compiler; the encoding is not a documented interchange format.
- **A fixed-point integer** — for example, the offset in units of 1/1000 stored as `int32_t`. This has a defined range and resolution, which a blob of raw bytes does not.

Arduino-ESP32's `Preferences` wrapper exposes `putFloat` / `getFloat` against the same NVS partition; the wrapper performs the serialisation that the 5.x C API leaves to the caller.

## First boot as a normal state, not an error

A freshly flashed board has no `nvs` entries. The distinguishing detail is that **NVS reports a missing namespace and a missing key with `ESP_ERR_NVS_NOT_FOUND`, which is a return code and not a fault**. Treating it as a fault produces a device that refuses to boot until it has been calibrated; treating it as the signal to substitute a compiled-in default produces a device that runs with defined behaviour from the first power-up.

`nvs_flash_init` has its own two recoverable failures. **`ESP_ERR_NVS_NO_FREE_PAGES`** indicates the partition has no free page available, and **`ESP_ERR_NVS_NEW_VERSION_FOUND`** indicates the partition was written by a newer NVS format than the running library understands. Both are handled by erasing the partition with `nvs_flash_erase` and initialising again — which destroys every stored value, including the calibration.

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

The length check in `cal_load_offset` is not redundant with the error check. `nvs_get_blob` reports the stored length through the in/out `len` parameter, so **a key that exists but holds a value of a different size is detected by comparing the returned length against `sizeof(float)`** rather than by any type tag the caller can rely on.

## The Arduino path

`Preferences` collapses the same sequence against the same partition, with a native float accessor and a default supplied at the call site:

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

The namespace argument to `begin` is subject to the same 15-character limit as `nvs_open`, and the second argument selects read-only or read-write mode — opening read-only and then calling a `put*` method fails rather than writing.

## Encryption

NVS supports an **encrypted mode backed by flash encryption, using XTS-AES**, with the encryption keys held in a separate partition of subtype `nvs_keys`. When flash encryption is enabled, NVS encryption is enabled by default, and the `nvs_get_*` / `nvs_set_*` calls are unchanged for the caller: decryption happens below the API. The property this provides is that values read out of the flash chip by an attacker with physical access — a stored Wi-Fi credential, for example — are ciphertext rather than plaintext.

## Pitfalls

- **A value written without `nvs_commit` disappears on the next reset.** `nvs_set_*` stages the change; only the commit guarantees it reached flash. The staged value reads back correctly through the open handle, so a same-session read-after-write test passes while the device still loses the value.
- **A key or namespace name longer than 15 characters is rejected at runtime, not at compile time.** The symptom is a `set` or `open` call returning an error on a name that looks fine in the source.
- **`ESP_ERR_NVS_NOT_FOUND` treated as a fatal error bricks the first boot.** A device that has never been calibrated has no key; the code path must substitute the default.
- **The `ESP_ERR_NVS_NO_FREE_PAGES` recovery erases the calibration.** `nvs_flash_erase` clears the whole partition, so a device that hits this condition returns to defaults with no other warning.
- **Opening with `NVS_READONLY` and then writing fails.** The mode is fixed at `nvs_open` time; the write returns an error rather than promoting the handle.
- **A 4-byte blob carries no type information.** Changing the stored representation — from raw `float` to fixed-point `int32_t`, both 4 bytes — leaves old devices reading the new bytes as the old type, with no length mismatch to catch it.
- **Erasing flash with `idf.py erase-flash` removes the NVS partition contents along with the firmware.** Per-unit calibration is lost by a routine reflash unless it is read out and restored.
