---
title: "Testing ESP-IDF Projects: Unity On-Target, pytest-embedded, and the Linux Target"
date: 2026-08-15
track: iot-embedded
summary: "Firmware validated only by flashing and reading the serial monitor regresses silently under refactoring. ESP-IDF supplies three execution environments — Unity test cases running on the chip, the pytest-embedded plugin family driving devkits or QEMU from a host, and a preview Linux target that builds hardware-free components as a native binary — together with the continuous-integration wiring to run all three per push."
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

**Gist.** Firmware mixes code that genuinely requires silicon — inter-integrated circuit (I2C) bus timing, Wi-Fi, analogue-to-digital conversion — with code that does not: parsers, filters, cyclic redundancy check (CRC) routines, state machines. ESP-IDF separates the two by offering three execution environments for the same test sources: Unity cases running on the chip, an emulated machine under QEMU, and a native host binary built for the preview `linux` target. The cost is that the environments differ in fidelity and in what they can build, so the component boundary — which code may reach a register — becomes a structural constraint on the firmware rather than a stylistic preference.

The [SEN5x node](/articles/iot-embedded/2026-07-24-esp32-sen5x-air-quality-mqtt/) contains a CRC routine, a moving-average filter, a reconnect state machine, and a Message Queuing Telemetry Transport (MQTT) payload formatter. One of the four needs a real chip.

## Unity on-target: the base layer

ESP-IDF bundles the C testing framework Unity and wraps registration in a single macro. Tests live in a component's `test/` subdirectory in files whose names begin with `test`, alongside a minimal `CMakeLists.txt`:

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

**`TEST_CASE` registers the case in a global list; the framework issues `UNITY_BEGIN()` and `UNITY_END()` around the run.** The build product is a separate *test app* — a small firmware whose entry point runs the Unity runner. Once flashed, it presents an interactive menu over the serial port: cases can be selected by name, by index, by `[tag]`, or with `*` for all of them.

Two heavier macros cover cases a single straight-line run cannot express. **`TEST_CASE_MULTIPLE_STAGES` spans a deliberate reboot**, which is what deep-sleep wake and [over-the-air update rollback](/articles/iot-embedded/2026-07-26-esp32-ota-updates/) require: the case resumes at the next stage after the reset rather than restarting. **`TEST_CASE_MULTIPLE_DEVICES` runs one case across two boards**, synchronised through `unity_wait_for_signal` and `unity_send_signal` so neither device proceeds past a rendezvous point before the other reaches it.

The layout that scales keeps tests out of the application image: a `test_apps/` directory holding one or more standalone test apps, each pulling in the components under test. That is the convention ESP-IDF uses for its own tests.

## pytest-embedded: driving hardware from a host

An interactive serial menu cannot be consumed by a continuous-integration (CI) job. The [pytest-embedded](https://docs.espressif.com/projects/pytest-embedded/en/latest/) plugin family reduces the sequence "flash the test app, open the port, drive the menu, parse the results" to a pytest fixture. Functionality is split across packages: `pytest-embedded` core, `pytest-embedded-serial-esp` for esptool-based flashing and serial access, `pytest-embedded-idf` for knowledge of `build/` directories, `sdkconfig` and Unity output, and `pytest-embedded-qemu`. The test script sits beside the app:

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

The `dut` (device under test) fixture flashes the built app to the attached devkit. **`run_all_single_board_cases()` walks the Unity menu and reports each `TEST_CASE` as a separate pytest result**, so a failure names one case rather than the whole firmware, and JUnit XML export follows from the standard pytest flag. `dut.expect()` matches a regular expression against the serial stream in the manner of pexpect; end-to-end assertions that are not Unity cases at all — boot, network join, first publish — are written the same way.

## Fidelity ladder: hardware, QEMU, Linux

**Real devkit.** The only environment that exercises I2C timing, Wi-Fi, ADC behaviour and power states. Driver code has no substitute for it.

**QEMU.** Espressif maintains a QEMU fork with machine support for several targets, among them ESP32 and ESP32-C3. `idf.py qemu monitor` runs the image unmodified, and `pytest-embedded-qemu` supplies the same `dut` fixture under `--embedded-services idf,qemu`. General-purpose input/output peripherals are not modelled in a way worth asserting against; boot flow, FreeRTOS scheduling, non-volatile storage (NVS) and the virtual Ethernet interface are, which covers application logic that never touches a sensor. **The environment needs no attached board, so it runs on a cloud CI runner.**

**Linux target.** ESP-IDF builds a subset of components as a native host binary, with POSIX/Linux implementations standing in for FreeRTOS and its neighbours:

```bash
idf.py --preview set-target linux
idf.py build
./build/test_aqi_filter.elf        # debuggable with plain gdb
```

This target remains marked preview and **only part of IDF is buildable under it**, which is the constraint that shapes the component split: a component compiles for `linux` only if every dependency it declares does. For hardware-free components — parsers, filters, [CRC routines](/articles/iot-embedded/2026-07-30-sen5x-raw-i2c-crc/), state machines — tests then run under an ordinary host test runner and debugger. IDF ships CMock-based mocking, so a component can be linked against a faked `esp_wifi` or driver application programming interface (API) instead of the real one.

The resulting strategy: move logic into hardware-free components tested on Linux, cover drivers with Unity on-target, and retain a QEMU smoke test for boot regressions.

## CI wiring

Host and QEMU tests run on GitHub-hosted runners using the `espressif/idf` Docker image. Hardware tests require a self-hosted runner with a devkit on the Universal Serial Bus (USB):

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    container: espressif/idf:release-v5.5
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

The build happens once on the cloud runner and the artifact is flashed on the physical one. **Labelling the self-hosted runner per chip keeps `--target` and runner selection consistent**; a mismatch flashes an image built for one chip onto another. IDF's own pytest setup pulls in `pytest-rerunfailures`, which retries cases whose failure comes from serial-port flakiness rather than firmware.

## Pitfalls

- **Tests placed inside the application component ship in the production image.** The `test/` sources are compiled into whatever app registers the component, inflating the binary and linking Unity into released firmware; a separate `test_apps/` build target is what keeps them out.
- **A component that declares a dependency unbuildable for `linux` fails the host build entirely**, not partially — the preview target covers a subset of IDF, so one `esp_wifi` reference in an otherwise pure filter removes the whole component from the fast loop.
- **`TEST_CASE_MULTIPLE_STAGES` cases that are aborted mid-sequence leave the device in an intermediate stage.** The next run resumes from the stored stage rather than the first, so results appear to skip assertions until the device is reset cleanly.
- **`dut.expect()` matches a regular expression, not a literal string.** Version banners and payloads containing `.`, `[`, `(` or `+` match more loosely than intended, and a test can pass against output it was never meant to accept.
- **A runner label that does not match the `--target` argument produces a flash-time failure, not a test failure**, so the CI report attributes the breakage to infrastructure rather than to the mismatch.
- **QEMU asserts nothing about GPIO or bus timing.** A driver test that passes under emulation and fails on silicon is the expected outcome, not a regression in the emulator.
