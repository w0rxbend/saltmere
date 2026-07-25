---
title: "Zephyr RTOS on an ESP32: west, device tree, and a real driver model"
date: 2026-07-25
track: iot-embedded
summary: "Zephyr gives the ESP32 a proper RTOS — device tree, Kconfig, and hundreds of upstream drivers — instead of Arduino globals or ESP-IDF's bespoke APIs. Here's a concrete from-zero blinky on esp32_devkitc/esp32/procpu with Zephyr 4.4."
reading_time: 5
tags: [esp32, zephyr, rtos, west, devicetree]
sources:
  - title: "Getting Started Guide — Zephyr Project Documentation"
    url: "https://docs.zephyrproject.org/latest/develop/getting_started/index.html"
  - title: "ESP32-DevKitC board page — Zephyr Project Documentation"
    url: "https://docs.zephyrproject.org/latest/boards/espressif/esp32_devkitc/doc/index.html"
  - title: "Zephyr 4.4.0 Release Notes"
    url: "https://docs.zephyrproject.org/latest/releases/release-notes-4.4.html"
  - title: "Zephyr Support Status — Espressif Developer Portal"
    url: "https://developer.espressif.com/software/zephyr-support-status/"
  - title: "Zephyr RTOS on ESP32 — Zephyr Project"
    url: "https://www.zephyrproject.org/zephyr-rtos-on-esp32/"
---

If your ESP32 project has outgrown Arduino's `loop()` and you want a scheduler, a device model, and drivers you don't write yourself — but ESP-IDF's own APIs feel like a walled garden — **Zephyr RTOS** is the third option. Espressif has contributed ESP32 support upstream since May 2020, so it is a mainline, official target: the same tree that runs on Nordic and STM32 parts builds for an ESP32 with one board argument. The current stable release is **Zephyr 4.4.0 (14 April 2026)**; 3.7 is the maintained LTS line.

## Set up the west workspace

Zephyr is a workspace of many repos glued together by `west`. Create one in a Python venv:

```bash
python3 -m venv ~/zephyrproject/.venv
source ~/zephyrproject/.venv/bin/activate
pip install west

west init ~/zephyrproject
cd ~/zephyrproject
west update
west zephyr-export
west packages pip --install
```

The ESP32 needs closed-source ROM/PHY blobs that aren't in the git tree. Fetch them once:

```bash
west blobs fetch hal_espressif
```

## Build and flash

The board target uses Zephyr's hardware-model-v2 naming — `board/soc/core`. For a classic ESP32 DevKitC that is **`esp32_devkitc/esp32/procpu`** (the protocol CPU; the second core is `.../appcpu`):

```bash
cd ~/zephyrproject/zephyr
west build -p always -b esp32_devkitc/esp32/procpu samples/basic/blinky
west flash
```

One gotcha: the bare `esp32_devkitc` has no user LED wired to a fixed pin, so `blinky` won't find its `led0` alias out of the box. That is a feature, not a bug — it's where the device tree earns its keep.

## Describe the hardware in an overlay

Instead of hard-coding a pin, you declare it. Drop an `app.overlay` beside your app (or under `boards/`) that adds an `led0` alias on GPIO2:

```dts
/ {
    aliases {
        led0 = &user_led;
    };
    leds {
        compatible = "gpio-leds";
        user_led: led_0 {
            gpios = <&gpio0 2 GPIO_ACTIVE_HIGH>;
            label = "User LED";
        };
    };
};
```

Now the sample's C code is portable — it never mentions GPIO2, only the alias:

```c
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>

#define LED0_NODE DT_ALIAS(led0)
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(LED0_NODE, gpios);

int main(void)
{
    if (!gpio_is_ready_dt(&led)) {
        return 0;
    }
    gpio_pin_configure_dt(&led, GPIO_OUTPUT_ACTIVE);

    while (1) {
        gpio_pin_toggle_dt(&led);
        k_msleep(500);
    }
    return 0;
}
```

Rebuild with the overlay applied and it blinks:

```bash
west build -p always -b esp32_devkitc/esp32/procpu samples/basic/blinky \
  -- -DEXTRA_DTC_OVERLAY_FILE=app.overlay
```

Move the same firmware to a Nordic dev kit later and you change one line — the overlay — not the C. That is the whole pitch: pins and buses live in device tree, feature flags live in Kconfig, and the driver subsystems (`sensor`, `i2c`, `gpio`, networking, USB) are shared across every board Zephyr supports.

**Try next:** swap the LED overlay for an I2C sensor node and read it through Zephyr's `sensor` API with `sensor_sample_fetch()` / `sensor_channel_get()` — the exact same driver code will then run unchanged on any other Zephyr board you point `west build -b` at.
