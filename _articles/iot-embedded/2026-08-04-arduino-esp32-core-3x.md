---
title: "Arduino-ESP32 3.x: what the move to ESP-IDF 5.x changed in sensor sketches"
date: 2026-08-04
track: iot-embedded
summary: "Arduino-ESP32 3.0 rebased the core from ESP-IDF 4.4 onto 5.1+, changing the arity and addressing model of the timer, LEDC and ADC APIs that sensor sketches depend on. Concrete before/after signatures for the calls that fail to compile, and the mechanics of pinning an exact core version."
reading_time: 6
tags: [esp32, arduino, esp-idf, migration, timer, ledc, adc]
sources:
  - title: "Migration from 2.x to 3.0 — Arduino-ESP32 documentation"
    url: "https://docs.espressif.com/projects/arduino-esp32/en/latest/migration_guides/2.x_to_3.0.html"
  - title: "Arduino-ESP32 Timer API reference"
    url: "https://docs.espressif.com/projects/arduino-esp32/en/latest/api/timer.html"
  - title: "Releases — espressif/arduino-esp32 (GitHub)"
    url: "https://github.com/espressif/arduino-esp32/releases"
  - title: "Announcing the Arduino ESP32 Core version 3.0.0 — Espressif Developer Portal"
    url: "https://developer.espressif.com/blog/2023/11/announcing-the-arduino-esp32-core-version-3-0-0/"
  - title: "pioarduino/platform-espressif32 (community PlatformIO platform for core 3.x)"
    url: "https://github.com/pioarduino/platform-espressif32"
---

**Gist.** Arduino-ESP32 is an Arduino personality layered over Espressif's ESP-IDF (IoT Development Framework) software development kit; through the 2.x line that SDK was IDF 4.4. Core **3.0.0** rebased the layer onto **ESP-IDF 5.1+**, which brought newer silicon and IDF component reuse into reach of `.ino` sketches. The cost is that the peripheral wrappers a sensor sketch calls most often — hardware timer, LEDC pulse-width modulation, and the analog-to-digital converter (ADC) tuning helpers — changed arity and addressing model, so affected sketches fail at compile time rather than degrading quietly.

The IDF baseline is not fixed across the line. **3.0.x shipped on IDF 5.1**, and later minor releases of the core moved up through the IDF 5.x series — the **3.3.x** releases are built on **IDF 5.5**. Each core release pins one IDF minor version, and that pin is recorded in the release notes for that version. The accurate mental model is therefore "3.x is IDF 5.x, with the exact IDF minor version fixed per core release" — not a single stable IDF baseline across the line.

## What the 5.x baseline makes available

Two capabilities are gated on the newer baseline. The first is silicon: IDF 5.x is where support for the **ESP32-C6** (Wi-Fi 6, Thread, Zigbee), the **ESP32-H2** (802.15.4 radio, no Wi-Fi) and the **ESP32-P4** application processor lives, so those parts become addressable from Arduino sketches only on core 3.x. The second is composition: a 3.x sketch can pull in **ESP-IDF components** from the Component Registry, so a managed IDF driver can be consumed from an `.ino` project rather than forcing a rewrite down at the bare IDF level.

Against that, three APIs present in most sensor sketches have to be relearned.

## The timer API: frequency-addressed instead of prescaler-addressed

The hardware-timer wrapper is the most frequently hit break, because paced sampling is normally driven by a timer interrupt rather than by `delay()`. **The 2.x API is expressed in prescaler ticks; the 3.x API is expressed in Hz and derives the divider internally.** Because the signatures changed arity, the failure mode is a hard compile error — there is no silently-wrong-rate version of this migration.

```cpp
// ---- Arduino-ESP32 2.x (IDF 4.4) ----
hw_timer_t *timer = NULL;

void IRAM_ATTR onSample() { /* set sensor flag, etc. */ }

void setup() {
  // timer 0, prescaler 80 -> 1 MHz tick, count up
  timer = timerBegin(0, 80, true);
  timerAttachInterrupt(timer, &onSample, true);   // edge = true
  timerAlarmWrite(timer, 500000, true);           // 500000 us, autoreload
  timerAlarmEnable(timer);
}
```

```cpp
// ---- Arduino-ESP32 3.x (IDF 5.x) ----
hw_timer_t *timer = NULL;

void IRAM_ATTR onSample() { /* set sensor flag, etc. */ }

void setup() {
  // tick frequency stated directly: 1 MHz
  timer = timerBegin(1000000);
  timerAttachInterrupt(timer, &onSample);         // no edge parameter
  // alarm at 500000 ticks (= 0.5 s), autoreload, reload count 0
  timerAlarm(timer, 500000, true, 0);
}
```

Three differences, all recorded in the official migration guide and the 3.x Timer API reference:

- **`timerBegin` drops from three arguments to one.** `(num, divider, countUp)` becomes `timerBegin(uint32_t frequency)`. The caller names the desired resolution; **the core selects a free timer and computes the divider across the available clock sources**, so the timer index is no longer part of the sketch's state.
- **`timerAttachInterrupt` loses its trailing edge argument,** becoming `timerAttachInterrupt(hw_timer_t*, void(*)(void))`.
- **The two-call arm sequence collapses into one.** `timerAlarmWrite()` followed by `timerAlarmEnable()` becomes `timerAlarm(timer, alarm_value, autoreload, reload_count)`, which writes and enables together.

The residual hazard is semantic rather than syntactic. **The alarm value is counted in ticks of the frequency passed to `timerBegin`, not in microseconds.** In the pair above the literal `500000` still denotes 0.5 s, but only because the requested tick frequency happens to be 1 MHz. A sketch ported with a different `timerBegin` argument and an unchanged alarm literal compiles cleanly and runs at the wrong rate.

## LEDC and ADC: pin-first addressing, and removed tuning functions

LEDC, the PWM peripheral used to drive a fan, a heater or a status LED, received the same call-merging treatment. **The 2.x flow is channel-oriented and the 3.x flow is pin-oriented.** Under 2.x, `ledcSetup(channel, freq, resolution)` configured a channel, `ledcAttachPin(pin, channel)` bound it to a pin, and duty was written to the *channel*. Under 3.x, `ledcAttach(pin, freq, resolution)` performs both steps and `ledcWrite(pin, duty)` addresses the *pin*; **channel assignment is managed by the core and no longer appears in sketch code**. The sequence `ledcSetup(0, 8000, 12); ledcAttachPin(26, 0); ledcWrite(0, duty);` becomes `ledcAttach(26, 8000, 12); ledcWrite(26, duty);`.

The ADC change is easier to miss because the everyday entry points are untouched: `analogRead(pin)`, `analogReadResolution(bits)` and `analogSetAttenuation()` all survive, so ordinary sensor reads keep compiling. **What the migration removed are the low-level tuning functions: `analogSetClockDiv`, `adcAttachPin` and `analogSetVRefPin` no longer exist.** A sketch that used those to suppress ADC noise has no drop-in replacement and needs a different approach.

The added capability is **`analogContinuous()`** — a direct-memory-access-backed continuous conversion mode exposed through `analogContinuous()`, `analogContinuousStart()` and `analogContinuousRead()`. It streams conversions instead of blocking on one-shot reads, which suits steady-rate sampling of an analog gas sensor or a microphone. This is the IDF 5.x ADC continuous driver surfaced into the Arduino layer.

RMT (the remote-control transceiver peripheral, commonly used for addressable LEDs and infrared) and the digital-to-analog converter helpers were reworked as well: **`rmtInit`/`rmtWrite` gained an explicit resolution parameter and changed their parameter lists**, so any sketch built directly on RMT requires review rather than a recompile.

## Pinning the core version

Because the breaks are compile-time and unconditional, the operational control is choosing which core a build compiles against, so that an integrated development environment (IDE) auto-update cannot move a field node from 2.x to 3.x mid-project.

In the **Arduino IDE**, Boards Manager exposes every published version in the "esp32 by Espressif Systems" entry; selecting an explicit version — a specific 3.x release, or a deliberate hold at the final 2.x release, 2.0.17 — rather than "latest" is what makes the choice durable. From the command line, **arduino-cli** pins explicitly:

```bash
arduino-cli core install esp32:esp32@3.3.0
```

**PlatformIO is the exception.** The official `espressif32` platform lagged the 3.x rebase for an extended period, and the community **pioarduino** fork became the route to current cores. In `platformio.ini` the `platform` key points at a specific pioarduino release tag:

```ini
[env:esp32dev]
platform = https://github.com/pioarduino/platform-espressif32/releases/download/54.03.20/platform-espressif32.zip
framework = arduino
board = esp32dev
```

Pinning the tag and committing the configuration file makes the build reproducible across machines and continuous-integration runners, which is the requirement when a deployed sensor node must be rebuildable a year later.

A low-risk migration order is to install a 3.x core alongside the existing one, port only the timer block of a single known-good 2.x sketch, and confirm the sampling interrupt fires at the original rate before changing anything else.

## Pitfalls

- **An alarm literal carried over unchanged runs at the wrong rate.** `timerAlarm` counts ticks of the frequency given to `timerBegin`; a port that copies `500000` from a 2.x sketch but requests a tick frequency other than 1 MHz compiles cleanly and samples at the wrong interval.
- **`ledcWrite` silently addresses a different resource after the port.** In 2.x its first argument is a channel number, in 3.x a pin number; a sketch using `ledcWrite(0, duty)` remains valid syntax under 3.x but now refers to GPIO 0.
- **ADC noise-tuning calls disappear rather than deprecate.** `analogSetClockDiv`, `adcAttachPin` and `analogSetVRefPin` were removed, so a sketch relying on them fails to compile with no substitute call to swap in.
- **RMT-based sketches fail on parameter shape, not on absence.** `rmtInit` and `rmtWrite` still exist but take different parameters including an explicit resolution, so an addressable-LED or infrared sketch cannot be migrated by recompiling.
- **"Core 3.x" does not name one IDF version.** 3.0.x is built on IDF 5.1 and 3.3.x on IDF 5.5, so behaviour attributed to the core may in fact depend on which IDF minor version that core release pinned.
- **Selecting "latest" in Boards Manager is not a pin.** A subsequent IDE update can move a project across the 2.x/3.x boundary, at which point the timer and LEDC calls stop compiling.
