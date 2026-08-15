---
title: "Testing ESP-IDF Projects: Unity On-Target, pytest-embedded, and the Linux Escape Hatch"
date: 2026-08-15
track: iot-embedded
summary: "Firmware you only test by flashing and squinting at the serial monitor will regress the moment you refactor. ESP-IDF has a real testing story: Unity test cases running on the chip, the pytest-embedded plugin family driving devkits (or QEMU) from Python, and a preview Linux target that runs your pure logic as a host binary — plus the GitHub Actions wiring to run it all on every push."
reading_time: 6
tags: [esp32, esp-idf, testing, unity, pytest-embedded, qemu, ci]
sources:
  - title: "ESP-IDF Programming Guide — Unit Testing in ESP32"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/unit-tests.html"
  - title: "ESP-IDF Programming Guide — pytest in ESP-IDF"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/contribute/esp-idf-tests-with-pytest.html"
  - title: "pytest-embedded documentation (espressif/pytest-embedded)"
    url: "https://docs.espressif.com/projects/pytest-embedded/en/latest/"
  - title: "ESP-IDF Programming Guide — Running ESP-IDF Applications on Host"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/host-apps.html"
  - title: "ESP-IDF Programming Guide — QEMU Emulator"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/tools/qemu.html"
---

The [SEN5x node](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt/) has a CRC routine, a moving-average filter, a reconnect state machine, and an MQTT payload formatter. Exactly one of those needs a real chip to test. ESP-IDF's testing stack lets you split the difference: Unity for on-target tests, pytest-embedded to orchestrate them from a host, and a Linux target for the logic that never touches a register.

## Unity on-target: the base layer

ESP-IDF bundles the C testing framework Unity and wraps it so a test is one macro. Tests live in a component's `test/` subdirectory (filenames starting with `test`), with a minimal `CMakeLists.txt`:

```c
// components/aqi_filter/test/test_aqi_filter.c
#include "unity.h"
#include "aqi_filter.h"

TEST_CASE("EMA filter converges on constant input", "[aqi_filter]")
{
    ema_t f;
    ema_init(&f, 0.2f);
    for (int i = 0; i < 100; i++) ema_update(&f, 42.0f);
    TEST_ASSERT_FLOAT_WITHIN(0.01f, 42.0f, ema_value(&f));
}
```

```cmake
idf_component_register(SRC_DIRS "." INCLUDE_DIRS "." REQUIRES unity)
```

`TEST_CASE` registers the test in a global list; the framework calls `UNITY_BEGIN()`/`UNITY_END()` for you. You then build a *test app* — a small firmware whose `main` just runs the Unity runner — flash it, and get an interactive menu over serial: run by name, by index, by `[tag]`, or `*` for everything. There are heavier macros too: `TEST_CASE_MULTIPLE_STAGES` for tests that span a deliberate reboot (deep-sleep wake, [OTA rollback](/articles/iot-embedded/2026-07-26-esp32-ota-updates/)) and `TEST_CASE_MULTIPLE_DEVICES` for two boards talking to each other, synchronized with `unity_wait_for_signal`/`unity_send_signal`.

The layout that scales: don't cram tests into your application. Keep a `test_apps/` directory with one or more standalone test apps, each pulling in the components under test. That's the convention ESP-IDF itself uses, and it keeps test code out of production images.

## pytest-embedded: driving the hardware from Python

An interactive serial menu is useless in CI. The [pytest-embedded](https://docs.espressif.com/projects/pytest-embedded/en/latest/) plugin family (2.x at the time of writing) turns "flash the test app, open the port, run the menu, parse the results" into a pytest fixture. The pieces are separate packages: `pytest-embedded` core, `pytest-embedded-serial-esp` (esptool-based flashing and serial), `pytest-embedded-idf` (understands `build/` dirs, sdkconfig, Unity output), and `pytest-embedded-qemu`. A test script sits next to the app:

```python
# test_apps/aqi/pytest_aqi_filter.py
import pytest

@pytest.mark.parametrize('target', ['esp32', 'esp32s3'], indirect=True)
@pytest.mark.generic
def test_unity_cases(dut) -> None:
    dut.run_all_single_board_cases(group='aqi_filter')

def test_boot_banner(dut) -> None:
    dut.expect('aqi-node: firmware v')     # regex against serial output
```

```bash
pip install pytest-embedded pytest-embedded-serial-esp pytest-embedded-idf
idf.py -C test_apps/aqi build
pytest test_apps/aqi --target esp32 --embedded-services esp,idf
```

The `dut` fixture flashes the built app to whatever devkit is plugged in, and `run_all_single_board_cases()` walks the Unity menu itself, reporting each `TEST_CASE` as an individual pytest result — JUnit XML and all. `dut.expect()` is pexpect against the serial stream, which is also how you write end-to-end tests ("boots, joins Wi-Fi, publishes within 30 s") that aren't Unity tests at all.

## Real hardware, QEMU, or Linux?

Three execution environments, in decreasing order of fidelity:

**Real devkit** is the only thing that tests I2C timing, Wi-Fi, ADC behavior, and power. You need it for driver code, full stop.

**QEMU**: Espressif maintains a QEMU fork with ESP32, ESP32-C3, and ESP32-S3 machine support; `idf.py qemu monitor` runs your image unmodified, and `pytest-embedded-qemu` slots it under the same `dut` fixture (`--embedded-services idf,qemu`). No GPIO peripherals worth trusting, but boot flow, FreeRTOS scheduling, NVS, and the virtual Ethernet are enough for a surprising amount of application logic — and it runs on any CI machine.

**Linux target**: the fastest loop of all. ESP-IDF can build a subset of components as a native host binary — POSIX/Linux implementations of FreeRTOS and friends stand in for the real thing:

```bash
idf.py --preview set-target linux
idf.py build
./build/test_aqi_filter.elf        # runs in milliseconds, debuggable with plain gdb
```

This is still marked preview and only a slice of IDF is buildable, but for pure-logic components (parsers, filters, [CRC routines](/articles/iot-embedded/2026-07-30-sen5x-raw-i2c-crc/), state machines) it means tests run in your editor's test runner like any C project, and IDF ships CMock-based mocking so a component can be tested against a faked `esp_wifi` or driver API.

The strategy that follows: push as much logic as possible into hardware-free components tested on Linux, cover drivers with Unity on-target, and keep a thin QEMU smoke test for boot regressions.

## CI wiring: Actions plus a devkit on a shelf

Host and QEMU tests run fine on GitHub-hosted runners with the `espressif/idf` Docker image. Hardware tests need a self-hosted runner with a devkit on USB — a Pi or an old laptop with an ESP32-S3 board taped to it is genuinely enough:

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    container: espressif/idf:release-v6.0
    steps:
      - uses: actions/checkout@v4
      - run: idf.py -C test_apps/aqi build
      - uses: actions/upload-artifact@v4
        with: { name: test-app, path: test_apps/aqi/build }
  hw-test:
    needs: build
    runs-on: [self-hosted, esp32]      # runner with the devkit attached
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with: { name: test-app, path: test_apps/aqi/build }
      - run: pytest test_apps/aqi --target esp32 --embedded-services esp,idf --junitxml=report.xml
```

Build once on the fast cloud runner, flash and test on the slow physical one. Label the runner per chip so `--target` and the runner selection stay in sync, and add `pytest-rerunfailures` (pulled in by IDF's own pytest setup) because serial ports flake.

None of this is exotic anymore — it's the same stack Espressif runs against thousands of boards in their own CI, scaled down to one devkit and a cron-triggered workflow.

**Try next:** extract one pure function from your node firmware into its own component, give it a `test/` directory with three Unity cases, and get them running both on a devkit via `pytest --target esp32` and as a Linux host binary — then time the two loops.
