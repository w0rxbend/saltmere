---
title: "HLK-LD2410 mmWave Presence on the ESP32: Seeing a Still Human"
date: 2026-08-15
track: iot-embedded
summary: "PIR sensors go blind the moment you sit still; the HLK-LD2410's 24 GHz FMCW radar keeps seeing you from breathing motion alone. The module's gate model (nine 0.75 m gates, ~6 m range), the 256000-baud UART protocol with its F4 F3 F2 F1 data frames, an Arduino parser, per-gate sensitivity tuning via engineering mode, and the ESPHome ld2410 component as the sane default. Plus where the LD2412 and multi-target LD2450 fit."
reading_time: 6
tags: [ld2410, mmwave, presence-detection, esp32, uart, esphome, home-assistant]
sources:
  - title: "HLK-LD2410 Serial Communication Protocol v1.02 (Hi-Link)"
    url: "https://www.sudo.is/docs/esphome/components/ld2410/HLK-LD2410_Serial_Communication_Protocol_v1.02.pdf"
  - title: "LD2410 Sensor — ESPHome documentation"
    url: "https://esphome.io/components/sensor/ld2410/"
  - title: "ESP32 + LD2410 pinout, wiring and code — ESPBoards"
    url: "https://www.espboards.dev/sensors/ld2410/"
  - title: "mgiesen/LD2410 — Arduino library with UART interface and web dashboard"
    url: "https://github.com/mgiesen/LD2410"
---

Every PIR-based "occupancy" automation has the same failure mode: you sit down to read, stop waving your arms, and three minutes later the lights go out. A **PIR** sensor is a pyroelectric element that only responds to *change* in infrared — a warm body holding still is indistinguishable from an empty room. The **HLK-LD2410** takes the opposite approach: it's a **24 GHz FMCW radar** (frequency-modulated continuous wave) that measures Doppler shifts down to the millimetre scale of a chest rising and falling. A sleeping human is still a detected human. At around $3–5 for the bare module, it has become the default answer to "how do I know the room is actually occupied," and it talks plain UART to an ESP32.

## Gates, and two kinds of energy

The LD2410 divides the space in front of it into **distance gates** of **0.75 m** each, nine gates (0–8) for a maximum range of about **6 m**. For every gate it continuously computes two values: **moving energy** (large Doppler shifts — walking, gesturing) and **static energy** (micro-motion — breathing, small posture shifts). Each gate has an independent sensitivity threshold from 0–100 for each energy type; a target is reported when a gate's energy exceeds its threshold. Detection ends only after a configurable **"no-one" duration** (0–65535 s, default 5 s) with nothing above threshold — this is the debounce that keeps a bedroom sensor from flapping.

This per-gate model is the whole tuning story. Gate 0–1 thresholds handle the desk right in front of the sensor; gate 7–8 thresholds decide whether the hallway beyond the door counts. A fan at 3 m is a gate-4 problem you solve with a gate-4 threshold, not by desensitising the whole device.

## Wiring and the UART protocol

The module wants **5 V** on VCC (there's an onboard regulator) but its TX/RX are **3.3 V TTL** — safe to wire directly to any ESP32 UART. Default serial settings are **256000 baud, 8N1**, which is why softserial on older MCUs struggles and the ESP32's hardware UARTs don't care.

Two frame families flow over that link. **Command frames** (host → module, and their ACKs) are bracketed by header `FD FC FB FA` and tail `04 03 02 01`, with a 2-byte little-endian length and a command word — `0x0060` sets max gates and the no-one duration, `0x0064` sets per-gate sensitivity (gate `0xFFFF` means "all gates"). **Data frames** (module → host, ~10 Hz) use header `F4 F3 F2 F1` and tail `F8 F7 F6 F5`. The payload starts with `0xAA`, then a **target state** byte — `0x00` none, `0x01` moving, `0x02` stationary, `0x03` both — followed by little-endian 16-bit moving-target distance (cm) and energy, static-target distance and energy, and overall detection distance, ending `55 00`.

A minimal Arduino-core parser for the basic frame:

```cpp
HardwareSerial radar(1);

void setup() {
  Serial.begin(115200);
  radar.begin(256000, SERIAL_8N1, 16, 17);  // RX=16 <- LD2410 TX, TX=17 -> LD2410 RX
}

void loop() {
  static uint8_t buf[64];
  static size_t n = 0;
  while (radar.available()) {
    buf[n++] = radar.read();
    if (n >= 4 && memcmp(buf + n - 4, "\xF8\xF7\xF6\xF5", 4) == 0) {
      if (n >= 23 && memcmp(buf, "\xF4\xF3\xF2\xF1", 4) == 0 && buf[6] == 0x02 && buf[7] == 0xAA) {
        uint8_t  state       = buf[8];                    // 0..3
        uint16_t move_cm     = buf[9]  | (buf[10] << 8);
        uint8_t  move_energy = buf[11];
        uint16_t still_cm    = buf[12] | (buf[13] << 8);
        uint8_t  still_energy= buf[14];
        Serial.printf("state=%u move=%ucm(%u) still=%ucm(%u)\n",
                      state, move_cm, move_energy, still_cm, still_energy);
      }
      n = 0;
    }
    if (n == sizeof(buf)) n = 0;
  }
}
```

Once frames parse, publishing `state != 0` as an occupancy topic is the same retained-JSON exercise as any other sensor — see the [MQTT discovery article](/articles/iot-embedded/2026-08-15-home-assistant-mqtt-discovery-esp32/) for making it appear in Home Assistant unaided. The framing-and-checksum discipline is the same habit as [parsing the PMS5003](/articles/iot-embedded/2026-07-31-pms5003-pm25-uart-esp32/), just with a different magic header.

## Tuning: engineering mode and the app

Blind threshold-guessing is miserable; use **engineering mode** (command `0x0062`), which extends each data frame with the live energy value of *every* gate. Watch the numbers with the room empty, note which gates the ceiling fan or curtain lights up, and set those gates' thresholds just above the noise. The **B** variant carries a Bluetooth radio and works with Hi-Link's **HLKRadarTool** phone app, which is exactly this workflow with sliders — tune over BLE, then let the UART consumer just read results. Config persists in the module's flash.

## The easy path: ESPHome

If the node's only job is presence, skip the parser entirely. ESPHome has a native `ld2410` component covering the full protocol including engineering mode and runtime tuning from the Home Assistant UI:

```yaml
uart:
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 256000
  parity: NONE
  stop_bits: 1

ld2410:

binary_sensor:
  - platform: ld2410
    has_target:
      name: Presence
    has_still_target:
      name: Still Target

sensor:
  - platform: ld2410
    still_distance:
      name: Still Distance
    still_energy:
      name: Still Energy
```

The component also exposes `number:` entities for every gate threshold and the timeout — tunable live, no reflash. It slots straight into the same node pattern as the [ESPHome air-quality build](/articles/iot-embedded/2026-07-31-esphome-diy-air-quality-node/).

## Placement is half the battle

Radar sees motion, not humans. The classic false-positive sources: **fans** (huge moving energy — mask those gates or aim away), **curtains** near HVAC vents, oscillating monitors, and pets. More surprising: 24 GHz penetrates drywall, so a sensor on a shared wall happily detects the neighbouring room — through-wall detection is a feature for hiding the sensor behind a plastic plate and a bug for everything else. Mount at 1.5–2 m height, tilted slightly down, with the beam (roughly ±60° horizontal) covering the seating area and *not* covering the doorway to the hallway. Never behind metal.

## The family

| Module | Range | Targets | Notes |
|---|---|---|---|
| LD2410 | ~6 m | 1 | Bare module, UART only |
| LD2410B | ~6 m | 1 | Adds BLE + app tuning; the one to buy |
| LD2410C | ~6 m | 1 | Smaller pinout/form factor, BLE |
| LD2412 | ~9 m | 1 | Longer range, wider beam, newer firmware |
| LD2450 | ~6 m | up to 3 | Multi-target *tracking*: X/Y position, speed per target |

The **LD2450** is the interesting sibling: instead of presence-plus-distance it streams X/Y coordinates and velocity for up to three targets, enabling zone-based logic ("someone is at the desk" vs "someone walked past"). ESPHome supports it too, as `ld2450`.

**Try next:** wire an LD2410B to a spare ESP32, flash the ESPHome config above with engineering mode enabled, and spend ten minutes watching per-gate energies with the room empty — then set each gate's still threshold ~10 points above its idle noise and see how long a motionless you stays detected from the far side of the room.
