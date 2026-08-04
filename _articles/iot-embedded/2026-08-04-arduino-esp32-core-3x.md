---
title: "Arduino-ESP32 3.x: what changed under your sensor sketches when the core moved to ESP-IDF 5.x"
date: 2026-08-04
track: iot-embedded
summary: "Arduino-ESP32 3.0 rebased the whole core from ESP-IDF 4.4 onto 5.1+, and the timer, LEDC, and ADC APIs your sensor sketches call all changed shape in the process. Here's the concrete before/after for the code that breaks, and how to pin an exact core version so a working node stays working."
reading_time: 5
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

If you write ESP32 firmware as Arduino sketches — `setup()`, `loop()`, `analogRead()`, a hardware timer to pace your sampling — you have been living on top of ESP-IDF the whole time without seeing it. Arduino-ESP32 is a thin Arduino personality bolted onto Espressif's real SDK. For years that SDK was IDF 4.4. With **core 3.0.0**, released mid-2024, the whole thing was rebased onto **ESP-IDF 5.1+**, and that is not a cosmetic bump: it is the reason a pile of sensor-node sketches that compiled fine on 2.x throw errors the moment you update boards. This piece is about that Arduino layer specifically — not the raw IDF migration, but what a sketch author actually hits and how to control which version you get.

The current core as of this writing is **3.3.8, based on ESP-IDF v5.5.4** (released April 2026). The 3.x line has been tracking the IDF 5.5 series through its point releases; 3.0.x shipped on 5.1, and the target has climbed steadily since. So "3.x is IDF 5.x" is the mental model, with the exact IDF minor version pinned per core release.

## Why the rebase was worth breaking things over

The upside is chips. IDF 5.x is where the newer silicon lives, so core 3.x is what gives Arduino sketches first-class support for the **ESP32-C6** (Wi-Fi 6, Thread, Zigbee), **ESP32-H2** (802.15.4, no Wi-Fi), and the **ESP32-P4** application processor. On 2.x/IDF 4.4 those parts either didn't exist or were unusable from Arduino. The other structural win is that you can now pull **ESP-IDF components** — anything from the Component Registry — into an Arduino sketch, so a managed driver written for IDF drops into your `.ino` project instead of forcing you down to bare IDF. For a sensor node that means you can reach for a maintained sensor component and still keep the Arduino ergonomics.

That is the trade. In exchange you re-learn three APIs that show up in almost every sensor sketch.

## The timer change every polling sketch hits

The single most common break is the hardware-timer API, because so many nodes use a timer interrupt to sample a sensor at a fixed rate instead of leaning on `delay()`. The 2.x API made you think in prescaler ticks; the 3.x API makes you think in Hz and does the divider math itself. The signatures genuinely changed arity, so this is a hard compile error, not a warning.

Here is a typical 2.x timer setup — a 0.5-second sampling tick — and its 3.x equivalent:

```cpp
// ---- Arduino-ESP32 2.x (IDF 4.4) ----
hw_timer_t *timer = NULL;

void IRAM_ATTR onSample() { /* read sensor flag, etc. */ }

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

void IRAM_ATTR onSample() { /* read sensor flag, etc. */ }

void setup() {
  // pick the timer's tick frequency directly: 1 MHz
  timer = timerBegin(1000000);
  timerAttachInterrupt(timer, &onSample);         // no edge param
  // alarm at 500000 ticks (= 0.5 s), autoreload, reload count 0
  timerAlarm(timer, 500000, true, 0);
}
```

Three concrete differences, all confirmed in the official migration guide and the 3.x Timer API reference. `timerBegin` drops from three arguments `(num, divider, countUp)` to one, `timerBegin(uint32_t frequency)` — you name the resolution you want and the core allocates a free timer and computes the divider across clock sources. `timerAttachInterrupt` loses its trailing `edge` argument, becoming `timerAttachInterrupt(hw_timer_t*, void(*)(void))`. And the old two-call `timerAlarmWrite()` + `timerAlarmEnable()` pair collapses into one `timerAlarm(timer, alarm_value, autoreload, reload_count)` that arms and enables in a single call. Note the semantic shift in the alarm value: because you set the tick frequency to 1 MHz above, `500000` still means 0.5 s, but the number now reads against *your* chosen frequency rather than a prescaler you had to reason about.

## LEDC and ADC: pin-first, and some functions gone

The LEDC (PWM) API went through the same "merge two calls into one" cleanup, which matters if your node drives a fan, a heater, or a status LED. The 2.x flow was channel-oriented: `ledcSetup(channel, freq, resolution)` then `ledcAttachPin(pin, channel)`, and you wrote duty to the *channel*. In 3.x the channel disappears from your code — `ledcAttach(pin, freq, resolution)` does both steps, and `ledcWrite(pin, duty)` addresses the *pin*. So `ledcSetup(0, 8000, 12); ledcAttachPin(26, 0); ledcWrite(0, duty);` becomes simply `ledcAttach(26, 8000, 12); ledcWrite(26, duty);`. The core manages channel assignment for you.

The ADC is subtler because the everyday call didn't change: `analogRead(pin)`, `analogReadResolution(bits)`, and `analogSetAttenuation()` all still work, so basic sensor reads keep compiling. What the migration removed are the low-level tuning functions — `analogSetClockDiv`, `adcAttachPin`, and `analogSetVRefPin` are gone. If your sketch reached for those to fight ADC noise, you now do it differently. The genuinely *new* capability is `analogContinuous()` — a DMA-backed continuous ADC mode (`analogContinuous()`, `analogContinuousStart()`, `analogContinuousRead()`) that lets you stream conversions instead of blocking on one-shot reads, which is exactly what you want for sampling an analog gas sensor or a microphone at a steady rate. Under the hood this is the IDF 5.x ADC continuous driver surfaced into Arduino. RMT and the DAC helpers were reworked too (RMT's `rmtInit`/`rmtWrite` gained an explicit resolution and changed parameters), so any addressable-LED or IR sketch built on raw RMT needs a second look.

## Pinning the version so a working node stays working

Because the breaks are real, the practical skill is controlling *which* core you compile against — you do not want an IDE auto-update silently jumping a field node from 2.x to 3.x mid-project. In the **Arduino IDE**, Boards Manager lets you select any published version from the "esp32 by Espressif Systems" dropdown; pick 3.3.8 (or hold at 2.0.17 deliberately) rather than "latest." From the command line, **arduino-cli** pins explicitly:

```bash
arduino-cli core install esp32:esp32@3.3.8
```

**PlatformIO** is the one gotcha. The official `espressif32` platform lagged the 3.x rebase for a long time, so the community **pioarduino** fork became the way to get current cores. In `platformio.ini` you point `platform` at a specific pioarduino release tag:

```ini
[env:esp32dev]
platform = https://github.com/pioarduino/platform-espressif32/releases/download/54.03.20/platform-espressif32.zip
framework = arduino
board = esp32dev
```

Pin the tag, commit `platform.ini`, and your build is reproducible across machines and CI — which is the whole point when a sensor deployment has to be rebuildable a year from now.

**Try next:** take one working 2.x sketch that uses `timerBegin(0, 80, true)`, install core 3.3.8 alongside your current one with `arduino-cli core install esp32:esp32@3.3.8`, and port just the timer block using the diff above — confirm your sampling interrupt still fires at the same rate before touching anything else.
