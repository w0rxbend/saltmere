---
title: "Moving an ESP32 project to ESP-IDF 6.0 without a bad afternoon"
date: 2026-07-25
track: iot-embedded
summary: "ESP-IDF 6.0 landed in March 2026 with a real installer, picolibc as the default C library, and — the part that will actually break your build — the legacy peripheral drivers finally deleted. Here's the migration path that gets a real project (say, an air-quality node) compiling again."
reading_time: 5
tags: [esp32, esp-idf, embedded, migration, freertos, picolibc]
sources:
  - title: "Announcing ESP-IDF v6.0 — Espressif Developer Portal"
    url: "https://developer.espressif.com/blog/2026/03/idf-v6-0-release/"
  - title: "ESP-IDF v6.0 framework adds ESP32-C5/C61 support — CNX Software"
    url: "https://www.cnx-software.com/2026/03/24/esp-idf-v6-0-framework-adds-support-for-esp32-c5-and-esp32-c61-preview-for-esp32-h21-and-esp32-h4/"
  - title: "Espressif announces ESP-IDF v6.0"
    url: "https://www.espressif.com/en/news/ESP_IDF_6.0"
---

ESP-IDF 6.0 shipped in March 2026, and it's one of those releases where the headline features are pleasant and the *breaking* changes are what eat your afternoon. If you have a working project on 5.x — an air-quality node, say, driving a sensor over I2C and blinking an LED with RMT — here's what actually changes and how to get it green again.

## What's genuinely new

The **ESP-IDF Installation Manager (EIM)** replaces the pile of install scripts with one cross-platform tool (GUI and CLI) that manages multiple IDF versions side by side and works offline for CI. **Picolibc** is now the default C library instead of Newlib — smaller flash and RAM footprint, which matters on a 4 MB part; Newlib is still selectable in `menuconfig` if something depends on it. **MbedTLS** jumps to 4.x on the PSA Crypto API, and there's an `idf.py`-integrated **MCP server** so an AI assistant can drive builds. On silicon, **ESP32-C5 and ESP32-C61 graduate to full support**, with H21/H4 in preview.

## The change that breaks your build

The legacy peripheral drivers are **removed**, not deprecated: ADC, DAC, I2S, Timer Group, PCNT, MCPWM, RMT, and Temperature Sensor. If your code still includes `driver/rmt.h` or the old `driver/i2c.h` master API, it will not compile. The fix is to move to the `_new` driver families that have been the recommended path since 5.x.

Concretely, old I2C setup like this:

```c
// ESP-IDF 5.x legacy — gone in 6.0
i2c_config_t conf = { .mode = I2C_MODE_MASTER, .sda_io_num = 21,
                      .scl_io_num = 22, .master.clk_speed = 100000 };
i2c_param_config(I2C_NUM_0, &conf);
i2c_driver_install(I2C_NUM_0, conf.mode, 0, 0, 0);
```

becomes the bus/device model in `driver/i2c_master.h`:

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

Two more things will bite: **compiler warnings are now errors by default**, so latent `-Wall` noise fails the build (fix them, or set `-Wno-error=...` on the specific warning while you clean up), and some components **moved** — `wifi_provisioning` became `network_provisioning`, and `cJSON` and `esp-mqtt` now come from the Component Registry rather than being bundled.

## A migration path that works

1. **Install 6.0 beside your 5.x** with EIM — don't overwrite. You want to A/B compile.
2. **Point your project at it** and run `idf.py set-target esp32` fresh, then `idf.py build`. Read the errors top-down; the driver includes fail first.
3. **Pull moved components** explicitly. For MQTT on an IoT node:

   ```bash
   idf.py add-dependency "espressif/esp-mqtt"
   ```

4. **Port each legacy driver** to its `_new` API. Do one peripheral, rebuild, repeat — don't convert everything blind.
5. **Clear the warnings-as-errors** last, once it links.

Budget an hour for a small node, more if you leaned on I2S or RMT. The payoff is real: picolibc alone can claw back a few KB of RAM, and you're back on a supported branch getting security fixes.

**Try next:** spin up a throwaway project on the ESP32-C5 (newly full-supported) with `idf.py create-project`, build the I2C example above against a sensor you own, and confirm it enumerates on the bus. Migrating a *tiny* project first turns the big migration from guesswork into a checklist.
