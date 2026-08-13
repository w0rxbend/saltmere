---
title: "Test ESP32 firmware without hardware: Wokwi in the browser and in CI"
date: 2026-08-13
track: iot-embedded
summary: "Wokwi simulates a real ESP32 (Wi-Fi, GPIO, sensors) from a diagram.json, and wokwi-cli runs the same simulation headlessly so GitHub Actions can boot your firmware, press virtual buttons, and assert on serial output — no dev board on a desk required."
reading_time: 5
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

The slow part of embedded work isn't writing firmware — it's the flash-and-stare loop, and the fact that CI can't see your board. Wokwi closes both gaps. It runs a cycle-aware simulation of the ESP32 (plus S3/C3/C6 and friends), including Wi-Fi to the real internet, I2C/SPI/UART peripherals, and common breakout parts. The same engine runs in the browser, in a VS Code extension against your local build, and headlessly via `wokwi-cli` so a pull request can boot the firmware and fail if the serial output is wrong.

## The two files that define a simulation

A Wokwi project is just your compiled firmware plus two text files that live next to it.

`wokwi.toml` points the simulator at your build artifacts:

```toml
[wokwi]
version = 1
elf = ".pio/build/esp32/firmware.elf"
firmware = ".pio/build/esp32/firmware.bin"
```

`diagram.json` is the wiring — parts and connections. Here's an ESP32 devkit with a pushbutton, wired to serial:

```json
{
  "version": 1,
  "author": "you",
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

The `$serialMonitor` pseudo-part is what makes headless assertions possible — it wires the UART to the harness. For an air-quality node, swap the button for a `wokwi-*` sensor or drive a custom I2C chip.

## Scenarios: script the inputs, assert the output

A scenario YAML automates the run: wait for serial text, poke controls, delay, then assert. This one presses the button three times and checks the counter:

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

`wait-serial` is a hard assertion: if the string never appears before the timeout, the run fails with a non-zero exit code — exactly what CI needs.

## Running it: local vs CI

Install the CLI (currently **v0.26.1**, released 23 Feb 2026) and run against a directory:

```bash
curl -L https://wokwi.com/ci/install.sh | sh
wokwi-cli . --timeout 10000 --scenario button.test.yaml \
  --expect-text 'Button pressed 3 times' --fail-text 'panic'
```

| Surface | Use it for | Needs |
|---|---|---|
| Browser (wokwi.com) | Quick prototyping, sharing repro | Nothing |
| VS Code extension | Debugging your local build interactively | `wokwi.toml` + build |
| `wokwi-cli` | Headless runs, VCD export, linting | `WOKWI_CLI_TOKEN` |

In GitHub Actions, build first, then hand off to the action (it wraps the CLI):

```yaml
- name: Simulate with Wokwi
  uses: wokwi/wokwi-ci-action@v1
  with:
    token: ${{ secrets.WOKWI_CLI_TOKEN }}
    path: /                      # dir containing wokwi.toml
    timeout: 10000
    scenario: 'button.test.yaml'
    expect_text: 'Button pressed 3 times'
    fail_text: 'panic'
```

The token comes from the Wokwi CI dashboard, starts with `wok_`, and is 44 characters — store it as a repo secret. Because the simulation has real network access, you can even boot firmware that connects to Wi-Fi and publishes an MQTT message, then assert on the broker side.

The catch worth knowing: simulation is functional, not timing-accurate to the nanosecond, and not every exotic peripheral is modeled. But for regression-guarding boot logic, sensor parsing, state machines, and reconnect handling, it turns "did I break the fleet firmware?" into a green check.

**Try next:** add `wokwi.toml`, `diagram.json`, and a one-step `wait-serial` scenario to an existing ESP32 repo, wire the `wokwi/wokwi-ci-action@v1` step after your build job, and watch a PR fail when you delete the boot banner `println`.
