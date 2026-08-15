---
title: "Zephyr RTOS on an ESP32: west, device tree, and the driver model"
date: 2026-07-25
track: iot-embedded
summary: "Zephyr supplies the ESP32 with a scheduler, a device tree hardware description, Kconfig feature selection, and upstream drivers shared with every other supported board. A from-zero blinky on esp32_devkitc/esp32/procpu with Zephyr 4.4."
reading_time: 6
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

**Gist.** Firmware written against Arduino's `loop()` or against ESP-IDF's chip-specific application programming interfaces (APIs) binds the application to one vendor's hardware abstraction. Zephyr real-time operating system (RTOS) separates the three concerns that cause that binding: **hardware topology lives in a device tree, compile-time feature selection lives in Kconfig, and the application calls generic subsystem APIs** (`gpio`, `i2c`, `sensor`, networking, universal serial bus) that every supported board implements. The cost is a multi-repository workspace, a build system that must be told which board and which overlay to apply, and a device-tree layer that must be edited before hardware the board file does not describe becomes reachable.

Espressif maintains ESP32 support upstream, so the ESP32 is a mainline target: the tree that builds for Nordic and STMicroelectronics parts builds for an ESP32 by changing one board argument. The release described here is **Zephyr 4.4.0**; 3.7 is the maintained long-term support (LTS) line.

## The west workspace

Zephyr is not a single repository. It is a **manifest repository plus a set of modules** — hardware abstraction layers (HALs), vendor blobs, third-party libraries — resolved by the `west` meta-tool. `west init` clones the manifest; `west update` clones or checks out every module at the revision the manifest pins. That pinning is the invariant that makes builds reproducible: a workspace is fully described by the manifest revision.

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

`west zephyr-export` registers the Zephyr CMake package with the user's CMake package registry, which is how an out-of-tree application later finds the tree without an environment variable.

ESP32 targets additionally require **closed-source binaries that are not carried in the git tree** — the Espressif physical-layer (PHY) and wireless libraries. They are fetched separately and stored in the HAL module:

```bash
west blobs fetch hal_espressif
```

Skipping this step does not fail at clone time. It fails later, when a build needs one of those objects, because a blob the workspace never fetched is absent from the module directory.

## Board targets and the two cores

Zephyr's hardware model v2 names a target as `board/soc/cpucluster`. The classic ESP32 DevKitC exposes two Xtensa cores, and the target selects which one the image runs on: **`esp32_devkitc/esp32/procpu`** is the protocol CPU, `esp32_devkitc/esp32/appcpu` the second core. The core is part of the build identity, not a runtime option — an image built for one is not loadable on the other.

```bash
cd ~/zephyrproject/zephyr
west build -p always -b esp32_devkitc/esp32/procpu samples/basic/blinky
west flash
```

`-p always` forces a pristine build. Without it, a stale CMake cache retains the previous board and overlay selection, which is what makes an overlay edit appear to have no effect.

## Why blinky does not blink

The bare `esp32_devkitc` board definition **does not declare a user light-emitting diode (LED) on a fixed pin**, so no node in its device tree carries the `led0` alias. `samples/basic/blinky` resolves its pin through `DT_ALIAS(led0)`; with the alias absent the build fails at the device-tree macro rather than silently producing a dark board. This is the observable behaviour of the device-tree layer: **hardware the board file does not describe does not exist to the application**, and the resolution happens entirely at compile time.

## Supplying the missing node in an overlay

An overlay is a device-tree fragment merged into the board's tree before compilation. Adding an `led0` alias bound to GPIO2 makes the sample buildable without editing the sample:

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

Three parts carry the weight. **`compatible = "gpio-leds"`** selects which driver binding claims the node; the binding defines that the node's children each have a `gpios` property. **`&gpio0 2 GPIO_ACTIVE_HIGH`** is a phandle-and-specifier triple: the controller node, the pin index within that controller, and flags that record the electrical polarity. **The alias** is the indirection the application depends on, so the application never names GPIO2.

Because polarity is recorded in the device tree, `GPIO_OUTPUT_ACTIVE` in the C code means *logically asserted*, not *electrically high*. Changing `GPIO_ACTIVE_HIGH` to `GPIO_ACTIVE_LOW` inverts the driving of the pin without any change to the application.

```c
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>

#define LED0_NODE DT_ALIAS(led0)
/* Expanded at compile time into a const struct holding the port device
 * pointer, pin number and flags taken from the device tree node. */
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

`gpio_is_ready_dt()` checks that the controller device passed its initialisation stage before the pin is touched. Zephyr initialises devices in ordered levels during boot; a driver whose dependency failed leaves its device marked not-ready rather than removing it, so the check distinguishes a present-but-uninitialised controller from a working one.

`k_msleep(500)` yields the thread to the scheduler for the duration instead of spinning, which is the distinction from a busy-wait `loop()`: the core is free for other threads and for power management during those 500 ms.

The overlay is applied by passing it through to CMake:

```bash
west build -p always -b esp32_devkitc/esp32/procpu samples/basic/blinky \
  -- -DEXTRA_DTC_OVERLAY_FILE=app.overlay
```

Moving the same firmware to another Zephyr-supported board changes the overlay, not the C source: the pin and controller are data, the application logic is code.

## Pitfalls

- **`west update` was never run after `west init`.** The build fails with missing modules or missing HAL headers; the manifest repository alone contains no drivers.
- **`west blobs fetch hal_espressif` was skipped.** The tree clones and configures cleanly, then fails once a build needs one of the PHY or wireless objects, because those binaries are distributed outside git.
- **The board argument omits the core.** `-b esp32_devkitc` is not a hardware-model-v2 target; the target must name the SoC and core, as in `esp32_devkitc/esp32/procpu`.
- **An overlay edit appears to have no effect.** A non-pristine build reuses the cached CMake configuration, including the previous overlay list; `-p always` is what forces re-evaluation.
- **`blinky` fails to build on a bare `esp32_devkitc`.** The board's device tree declares no `led0` alias, and `DT_ALIAS(led0)` is resolved at compile time, so the absence is a build error rather than a runtime no-op.
- **Polarity is set in C rather than in the device tree.** Inverting the level inside the application defeats the abstraction; the `GPIO_ACTIVE_HIGH`/`GPIO_ACTIVE_LOW` flag on the `gpios` property is where board-specific wiring belongs.
- **An image built for `procpu` is flashed expecting the second core.** The core is part of the build identity; `appcpu` requires its own build target.
