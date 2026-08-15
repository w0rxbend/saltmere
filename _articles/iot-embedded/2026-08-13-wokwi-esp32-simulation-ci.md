---
title: "Testing ESP32 firmware without hardware: Wokwi in the browser and in CI"
date: 2026-08-13
track: iot-embedded
summary: "Wokwi simulates an ESP32 (Wi-Fi, GPIO, sensors) from a diagram.json, and wokwi-cli runs the same simulation headlessly so GitHub Actions can boot the firmware, actuate virtual controls, and assert on serial output without a development board."
reading_time: 6
tags: [esp32, wokwi, ci, testing, github-actions]
sources:
  - title: "Wokwi CLI (GitHub)"
    url: "https://github.com/wokwi/wokwi-cli"
  - title: "Using Wokwi in GitHub Actions"
    url: "https://docs.wokwi.com/wokwi-ci/github-actions"
  - title: "wokwi-ci-action README"
    url: "https://github.com/wokwi/wokwi-ci-action"
  - title: "ESP32 PlatformIO counter with Wokwi CI (reference repo)"
    url: "https://github.com/wokwi/platform-io-esp32-counter-ci"
  - title: "ESP-IDF: Wokwi third-party tool"
    url: "https://docs.espressif.com/projects/esp-idf/en/latest/esp32/third-party-tools/wokwi.html"
---

**Gist.** Firmware regressions are normally caught by flashing a physical board, which no continuous-integration (CI) runner has. Wokwi replaces the board with a simulation of the ESP32 family driven by two text files — a build-artifact manifest and a wiring diagram — and `wokwi-cli` executes that simulation headlessly, turning a scripted scenario into a process exit code. The cost is fidelity: the simulation reproduces functional behaviour and peripheral protocols, not nanosecond-accurate timing, and only modelled parts exist.

## What the simulator provides

Wokwi runs a functional simulation of the ESP32 and related Espressif parts (S3, C3, C6 among them), including **Wi-Fi with real internet access**, the I2C, SPI and UART peripherals, and a library of breakout components. The same engine is reachable through three surfaces: the browser at wokwi.com, a Visual Studio Code extension that attaches to a locally built binary, and the command-line interface `wokwi-cli`. Espressif documents Wokwi as a third-party tool for ESP-IDF. The simulator's input is the artifacts an ordinary IDF or PlatformIO build already emits, so **no separate host build or mocking layer is introduced**.

## The two files that define a simulation

A Wokwi project consists of the compiled firmware plus two text files placed beside it.

`wokwi.toml` names the build artifacts. The PlatformIO layout below references both a raw binary image and an ELF (executable and linkable format) file: `firmware` names the image loaded into the simulated flash, `elf` names the file carrying the symbols.

```toml
[wokwi]
version = 1
elf = ".pio/build/esp32/firmware.elf"
firmware = ".pio/build/esp32/firmware.bin"
```

`diagram.json` is the netlist: a list of parts and a list of connections between named pins. The example below is an ESP32 development kit with a pushbutton pulled to ground and the UART routed to the harness.

```json
{
  "version": 1,
  "author": "author",
  "editor": "wokwi",
  "parts": [
    { "type": "wokwi-esp32-devkit-v1", "id": "esp", "top": 0, "left": 0, "attrs": {} },
    { "type": "wokwi-pushbutton", "id": "btn1", "top": 16, "left": 192,
      "attrs": { "color": "green", "bounce": "0" } },
    { "type": "wokwi-gnd", "id": "gnd1", "top": 67, "left": 268, "attrs": {} }
  ],
  "connections": [
    [ "esp:TX0", "$serialMonitor:RX", "", [] ],
    [ "esp:RX0", "$serialMonitor:TX", "", [] ],
    [ "btn1:1.l", "esp:D5", "green", [ "h0" ] ],
    [ "btn1:2.r", "gnd1:GND", "black", [ "h0" ] ]
  ]
}
```

Two details in that file are load-bearing. The first is **`$serialMonitor`, a pseudo-part rather than a physical one**: connecting `esp:TX0` to `$serialMonitor:RX` exposes the UART stream to the harness, and without that connection a headless run has nothing to assert on. The second is the button's **`"bounce": "0"` attribute**, which disables the simulated contact bounce. With bounce enabled the model emits multiple edges per press, so firmware without debouncing counts one press several times; the attribute decides which of those two behaviours the test exercises. The `top`/`left` fields are layout coordinates only and do not affect electrical behaviour.

## Scenarios: scripted inputs, asserted outputs

A scenario file is a YAML list of steps executed in order against the running simulation. Steps wait for serial text, set a control on a part, or delay for a fixed simulated interval.

```yaml
name: Pushbutton counter test
version: 1
steps:
  - wait-serial: 'Pushbutton Counter'
  - set-control: { part-id: btn1, control: pressed, value: 1 }
  - delay: 100ms
  - set-control: { part-id: btn1, control: pressed, value: 0 }
  - delay: 200ms
  - wait-serial: 'Button pressed 3 times'
```

The control structure is the state machine of the test. `set-control` drives an input to a level and leaves it there; a press is therefore **two steps and a delay, not one event** — value 1, hold, value 0 — and firmware that triggers on a release edge sees nothing until the second `set-control` runs. `wait-serial` is a blocking assertion: execution stops until the literal string appears on the UART stream, and **if it never appears before the run-level timeout the process exits non-zero**. That exit code is the entire interface to CI. Note the asymmetry: a `wait-serial` that matches early passes immediately, so the delays bound how much simulated time each stimulus is given, not how long the assertion may take.

## Running it locally and in CI

The command-line interface is installed by script and pointed at the directory containing `wokwi.toml`:

```bash
curl -L https://wokwi.com/ci/install.sh | sh
wokwi-cli . --timeout 10000 --scenario button.test.yaml \
  --expect-text 'Button pressed 3 times' --fail-text 'panic'
```

`--timeout` is expressed in milliseconds of simulated time and caps the whole run. `--expect-text` and `--fail-text` are the degenerate case of a scenario — a single positive and a single negative match — and are usable without any scenario file at all. **`--fail-text` matters more than it appears**: a firmware that panics and reboots will often reprint its boot banner, so a run asserting only on the banner can pass while the device is crash-looping. Matching `panic` terminates the run at the first fault.

| Surface | Purpose | Requires |
|---|---|---|
| Browser (wokwi.com) | Prototyping, sharing a reproduction | Nothing |
| VS Code extension | Interactive debugging of a local build | `wokwi.toml` plus a build |
| `wokwi-cli` | Headless runs driven by a scenario or by text matches | `WOKWI_CLI_TOKEN` plus a build |

In GitHub Actions the firmware must be built by an earlier step; the action wraps the same CLI and does not compile anything.

{% raw %}
```yaml
- name: Simulate with Wokwi
  uses: wokwi/wokwi-ci-action@v1
  with:
    token: ${{ secrets.WOKWI_CLI_TOKEN }}
    path: /                      # directory containing wokwi.toml
    timeout: 10000
    scenario: 'button.test.yaml'
    expect_text: 'Button pressed 3 times'
    fail_text: 'panic'
```
{% endraw %}

The token is issued by the Wokwi CI dashboard and belongs in a repository secret. Because the simulated Wi-Fi stack reaches the real internet, a scenario can boot firmware that associates with a network, publishes an MQTT (Message Queuing Telemetry Transport) message, and be asserted on from the broker side rather than only through the UART.

The boundary of the technique is fidelity. The simulation is functional rather than timing-accurate to the nanosecond, and only modelled peripherals exist. What it does guard reliably is boot logic, sensor-frame parsing, state machines and reconnect handling — the failures that otherwise surface only after a fleet update.

## Pitfalls

- **A run passes while the device crash-loops.** The panic handler reboots, the boot banner reprints, and a bare `--expect-text` on that banner matches. Cause: no `--fail-text`/`fail_text` pattern covering `panic` or the guru-meditation output.
- **The scenario hangs until the timeout with no output captured.** Cause: `diagram.json` omits the `esp:TX0` → `$serialMonitor:RX` connection, so the UART stream never reaches the harness even though the firmware is transmitting.
- **A single simulated press registers as several.** Cause: the pushbutton's `bounce` attribute is left at its default rather than `"0"`, and the firmware performs no debouncing.
- **A press produces no reaction at all.** Cause: `set-control` sets a level, not an edge; the scenario asserted the value 1 without the matching value 0 step that firmware triggering on release requires.
- **The action fails immediately in a fork's pull request.** Cause: `secrets.WOKWI_CLI_TOKEN` is not exposed to workflows triggered from forks, so the token input arrives empty.
- **CI simulates stale firmware.** Cause: the paths in `wokwi.toml` point at a build directory the CI job never populated, or the build step ran after the simulation step; the simulator loads whatever binary is at that path without checking its age.
