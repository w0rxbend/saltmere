---
title: "Migrating an ESP32 project to ESP-IDF 6.0"
date: 2026-07-25
track: iot-embedded
summary: "ESP-IDF 6.0 shipped in March 2026 with a cross-platform installation manager, picolibc as the default C library, and the legacy peripheral drivers removed rather than deprecated. The removal is the change that stops an existing 5.x project from compiling; this is the mechanism behind it and the migration order that isolates each failure."
reading_time: 6
tags: [esp32, esp-idf, embedded, migration, freertos, picolibc]
sources:
  - title: "Announcing ESP-IDF v6.0 — Espressif Developer Portal"
    url: "https://developer.espressif.com/blog/2026/03/idf-v6-0-release/"
  - title: "ESP-IDF v6.0 framework adds ESP32-C5/C61 support — CNX Software"
    url: "https://www.cnx-software.com/2026/03/24/esp-idf-v6-0-framework-adds-support-for-esp32-c5-and-esp32-c61-preview-for-esp32-h21-and-esp32-h4/"
  - title: "Espressif announces ESP-IDF v6.0"
    url: "https://www.espressif.com/en/news/ESP_IDF_6.0"
---

**Gist.** ESP-IDF 6.0, released in March 2026, **removes the legacy peripheral drivers instead of deprecating them**, so a project on 5.x that includes `driver/rmt.h` or the legacy `driver/i2c.h` master interface fails at the preprocessor rather than emitting a warning. The migration replaces each legacy driver with its `_new` family, whose central change is an explicit **bus/device object model with handles** in place of the port-indexed global configuration used before. The cost is that every peripheral call site must be rewritten, and because warnings are now errors by default, latent diagnostics that a 5.x build tolerated become build failures at the same time.

## What the release changes

The **ESP-IDF Installation Manager (EIM)** replaces the previous collection of install scripts with a single cross-platform tool offering both a graphical and a command-line interface. It manages **multiple IDF versions side by side** and operates offline, which is the property that matters for continuous-integration images.

**Picolibc** becomes the default C library in place of **Newlib**, reducing flash and RAM footprint — a difference that is load-bearing on a 4 MB part. **Newlib remains selectable in `menuconfig`**, so a component with a Newlib-specific dependency is not blocked by the default.

**MbedTLS** moves to the 4.x series built on the **PSA Crypto application programming interface (API)**. An **MCP (Model Context Protocol) server** is integrated with `idf.py`, allowing an assistant process to drive builds. On silicon, **ESP32-C5 and ESP32-C61 reach full support**; ESP32-H21 and H4 are in preview.

## Why the build stops

The removed drivers include **ADC, DAC, I2S, Timer Group, PCNT, MCPWM, RMT and Temperature Sensor**, alongside the legacy I2C interface. Deprecation and removal fail differently, and the difference determines the shape of the migration:

- A **deprecated** header still exists. The translation unit compiles, the linker resolves the symbol, and the diagnostic is advisory. Migration can be deferred and staged.
- A **removed** header does not exist. The failure occurs at `#include`, before any semantic analysis of the file. Every subsequent error the compiler would have reported inside that file is suppressed, because compilation of that translation unit stops.

The practical consequence is that **the first build after switching toolchains reports the include failures and nothing else**. The list of errors is therefore not a measure of remaining work; it is the set of files whose real error count is still unknown. Each round of fixes exposes a new layer. This is the reason the migration is done one peripheral at a time rather than by converting every call site before the first rebuild.

The replacement `_new` driver families have been the documented path since 5.x, so a project already tracking current guidance has less to change.

## The model shift in the I2C driver

The legacy interface configured a **port** — a small integer naming a hardware controller — and then addressed devices per transaction:

```c
// ESP-IDF 5.x legacy — removed in 6.0
i2c_config_t conf = { .mode = I2C_MODE_MASTER, .sda_io_num = 21,
                      .scl_io_num = 22, .master.clk_speed = 100000 };
i2c_param_config(I2C_NUM_0, &conf);
i2c_driver_install(I2C_NUM_0, conf.mode, 0, 0, 0);
```

Two properties of this form matter. The clock speed is a **property of the port**, so every device on the bus shares it. And `I2C_NUM_0` is a **global name**: any code in the firmware can call `i2c_param_config` on the same port, and the last caller wins, with no compile-time or run-time indication that an earlier configuration was overwritten.

The 6.0 interface in `driver/i2c_master.h` separates the bus from the devices attached to it:

```c
// ESP-IDF 6.0
i2c_master_bus_config_t bus_cfg = {
    .i2c_port = I2C_NUM_0, .sda_io_num = 21, .scl_io_num = 22,
    .clk_source = I2C_CLK_SRC_DEFAULT, .glitch_ignore_cnt = 7,
};
i2c_master_bus_handle_t bus;
ESP_ERROR_CHECK(i2c_new_master_bus(&bus_cfg, &bus));

i2c_device_config_t dev_cfg = {
    .dev_addr_length = I2C_ADDR_BIT_LEN_7,
    .device_address = 0x69,            // e.g. a SEN5x sensor
    .scl_speed_hz = 100000,
};
i2c_master_dev_handle_t dev;
ESP_ERROR_CHECK(i2c_master_bus_add_device(bus, &dev_cfg, &dev));
```

`i2c_new_master_bus` returns a **handle**, and the device is registered against that handle rather than against a port number. Two consequences follow directly. **`scl_speed_hz` is now per device**, so a sensor limited to 100 kHz and a display capable of 400 kHz can share one bus without the slower part dictating the bus speed. And **ownership becomes explicit**: a second attempt to install a driver on an already-owned port is rejected by `i2c_new_master_bus` returning an error rather than silently replacing the previous configuration.

The `glitch_ignore_cnt` field configures the input filter width in bus clock cycles; the value 7 is the one used in Espressif's examples.

## Two secondary breakages

**Compiler warnings are errors by default.** A 5.x project that accumulated `-Wall` diagnostics without ever failing on them now fails on all of them at once, and these appear only after the include errors are resolved. Suppressing a specific diagnostic with `-Wno-error=<name>` keeps the build moving while the underlying issue is fixed; suppressing the whole class removes the signal.

**Components moved.** `wifi_provisioning` was renamed **`network_provisioning`**, and `cJSON` and `esp-mqtt` are pulled from the **Component Registry** rather than shipped inside the framework. A moved component produces the same include-time failure as a removed driver, so it belongs to the same first round of errors.

## Migration order

1. **Install 6.0 alongside the existing 5.x toolchain** using EIM. Side-by-side installation preserves a known-good build for comparison; overwriting removes the reference point.
2. **Run `idf.py set-target esp32` fresh, then `idf.py build`.** Reading errors top-down surfaces the driver includes first, because they abort their translation units before anything else in them is analysed.
3. **Add the moved components explicitly**, for example `idf.py add-dependency "espressif/esp-mqtt"`.
4. **Port one peripheral, rebuild, repeat.** Each rebuild reveals errors that the previous include failure was hiding, so converting every peripheral before the first rebuild proceeds without feedback.
5. **Clear the warnings-as-errors last**, once the image links.

Picolibc's smaller footprint recovers some RAM relative to Newlib; the magnitude depends on which library routines the firmware pulls in and is measured by comparing the two builds with `idf.py size`.

## Pitfalls

- Overwriting the 5.x installation instead of installing 6.0 beside it removes the ability to A/B compile, so a regression cannot be attributed to the migration rather than to an unrelated local change.
- Counting the errors in the first build as the remaining work underestimates it: a failed `#include` terminates its translation unit, so every other error in that file is unreported until the include is fixed.
- Converting every peripheral before rebuilding means the first rebuild reports errors from all of them simultaneously, with no way to tell which conversion introduced which failure.
- Carrying the legacy per-port clock speed into `i2c_master_bus_config_t` fails to compile: the speed field lives in `i2c_device_config_t` as `scl_speed_hz` in the 6.0 model.
- Globally disabling warnings-as-errors to reach a linking image discards the diagnostics that the change exists to surface, and the build no longer reports when new ones appear.
- Assuming `wifi_provisioning` still resolves produces an include error identical in form to a removed driver's, which can be misread as another legacy-driver problem; the component was renamed to `network_provisioning`.
- Relying on a Newlib-specific behaviour without reselecting Newlib in `menuconfig` fails against the picolibc default, and the failure appears at link or run time rather than at the include stage.
