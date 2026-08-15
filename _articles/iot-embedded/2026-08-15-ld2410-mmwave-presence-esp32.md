---
title: "HLK-LD2410 mmWave Presence on the ESP32: Detecting a Motionless Human"
date: 2026-08-15
summary: "Passive infrared sensors stop reporting once a body holds still; the HLK-LD2410's 24 GHz FMCW radar continues to report from breathing-scale motion. Covers the gate model (nine 0.75 m gates, roughly 6 m range), the 256000-baud UART protocol with its F4 F3 F2 F1 data frames, an Arduino-core parser, per-gate sensitivity tuning through engineering mode, the ESPHome ld2410 component, and where the LD2412 and multi-target LD2450 fit."
track: iot-embedded
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

**Gist.** A **passive infrared (PIR)** sensor is a pyroelectric element that responds only to *change* in incident infrared, so a warm body holding still becomes indistinguishable from an empty room and occupancy automations time out on a seated reader. The **HLK-LD2410** replaces that measurement with a **24 GHz frequency-modulated continuous-wave (FMCW) radar** that resolves Doppler shifts down to the chest movement of breathing, and reports presence per distance gate over a plain UART link. The cost is that radar detects *motion in space*, not people: fans, curtains and the room on the far side of a drywall partition all produce energy, so every deployment turns into a per-gate threshold and placement exercise.

## Gates, and two kinds of energy

The LD2410 partitions the space in front of it into **distance gates of 0.75 m each, gates 0–8**, for a maximum range of approximately **6 m**. For each gate the module continuously computes two quantities: **moving energy**, from large Doppler shifts such as walking or gesturing, and **static energy**, from micro-motion such as breathing and small posture shifts. **Each gate carries an independent sensitivity threshold in the range 0–100 for each of the two energy types**, and a target is reported when a gate's energy exceeds that gate's threshold.

The reported state does not clear at the instant energy drops. Detection ends only after a configurable **"no-one" duration (0–65535 s, default 5 s)** has elapsed with nothing above threshold. That timer is the debounce that prevents a bedroom sensor from flapping between occupied and empty on marginal energy.

The per-gate model is the entire tuning surface. **Gate 0 and gate 1 thresholds govern the desk directly in front of the module; gate 7 and gate 8 thresholds decide whether the hallway beyond the door registers.** A fan at 3 m falls in gate 4 and is addressed by raising the gate-4 threshold rather than desensitising the whole device.

## Wiring and the UART protocol

VCC expects **5 V** while TX and RX are **3.3 V TTL**, which permits direct connection to any ESP32 UART without level shifting. Default serial settings are **256000 baud, 8N1**. That rate is why a bit-banged software serial port on slower microcontrollers is marginal and why the ESP32's hardware UARTs are the appropriate consumer.

Two frame families travel over the link.

**Command frames** (host to module, and their acknowledgements) are bracketed by header `FD FC FB FA` and tail `04 03 02 01`, carrying a 2-byte little-endian length and a command word. `0x0060` sets the maximum gate count and the no-one duration; `0x0064` sets per-gate sensitivity, where **gate value `0xFFFF` addresses all gates at once**.

**Data frames** (module to host, emitted continuously without being polled) use header `F4 F3 F2 F1` and tail `F8 F7 F6 F5`. The payload opens with `0xAA`, then a **target-state byte**: `0x00` no target, `0x01` moving target, `0x02` stationary target, `0x03` both. The state byte is followed by little-endian 16-bit moving-target distance in centimetres and its energy, stationary-target distance and its energy, and an overall detection distance, terminating with `55 00`.

The load-bearing consequence for a parser is that **the tail sequence, not a length field, is the reliable resynchronisation point**: a parser that scans for `F8 F7 F6 F5`, then validates the header and the expected offsets, recovers from a truncated frame within one frame period.

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
      n = 0;                                              // resync on tail
    }
    if (n == sizeof(buf)) n = 0;                          // drop oversized garbage
  }
}
```

The byte at offset 6 is the data type, and the check `buf[6] == 0x02` is load-bearing: **an engineering-mode frame carries a different data type and inserts the per-gate energies before the tail**, so this parser silently discards every frame once engineering mode is enabled. A consumer that wants those energies must accept the engineering data type and read the gate array at its own offsets.

Once frames parse, publishing `state != 0` as an occupancy topic is the same retained-JSON exercise as any other sensor — see the [MQTT discovery article](/articles/iot-embedded/2026-08-15-home-assistant-mqtt-discovery-esp32/) for making the entity appear in Home Assistant without manual configuration. The framing-and-validation discipline matches [parsing the PMS5003](/articles/iot-embedded/2026-07-31-pms5003-pm25-uart-esp32/) with a different header sequence.

## Tuning through engineering mode

Threshold selection without observation is guesswork. **Engineering mode, entered with command `0x0062`, extends each data frame with the live energy value of every gate.** The procedure is to observe those values with the room unoccupied, identify which gates a ceiling fan or a moving curtain excites, and set those gates' thresholds above the observed idle noise.

The **B** variant carries a Bluetooth radio and works with Hi-Link's **HLKRadarTool** phone application, which exposes the same per-gate thresholds as sliders; tuning happens over Bluetooth Low Energy while the UART consumer reads only results. **Configuration persists in the module's flash**, so a tuned module retains its thresholds across power cycles and across reflashes of the host microcontroller.

## The ESPHome path

Where a node's only function is presence, the hand-written parser is unnecessary. ESPHome provides a native `ld2410` component covering the protocol including engineering mode and runtime tuning from the Home Assistant interface:

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

The component also exposes `number:` entities for every gate threshold and for the timeout, which makes tuning a runtime operation rather than a reflash. It follows the same node pattern as the [ESPHome air-quality build](/articles/iot-embedded/2026-07-31-esphome-diy-air-quality-node/).

## Placement

Radar detects motion, not humans, and the distinction drives siting. Recurring false-positive sources are **fans**, which produce large moving energy and are best handled by masking their gates or aiming away; curtains near heating, ventilation and air-conditioning (HVAC) vents; oscillating displays; and pets. Less intuitively, **24 GHz penetrates drywall**, so a module mounted on a shared wall detects activity in the adjoining room — the same property that permits concealment behind a plastic plate. Mounting at roughly torso height and tilting slightly downward, with the beam of roughly **±60°** covering the seating area and excluding the doorway, addresses most of these. Metal in front of the module blocks it.

## The family

| Module | Range | Targets | Notes |
|---|---|---|---|
| LD2410 | ~6 m | 1 | Bare module, UART only |
| LD2410B | ~6 m | 1 | Adds BLE and app tuning |
| LD2410C | ~6 m | 1 | Smaller pinout/form factor, BLE |
| LD2412 | longer than the LD2410 | 1 | More distance gates, separate protocol revision |
| LD2450 | ~6 m | up to 3 | Multi-target tracking: X/Y position and speed per target |

The **LD2450** differs in kind rather than degree: instead of presence plus a single distance it streams X/Y coordinates and velocity for up to three targets, which supports zone-based logic — distinguishing a target stationary at a desk from one traversing the room. ESPHome supports it as `ld2450`.

## Pitfalls

- **Lights extinguish on a seated occupant even with the LD2410 installed.** The still-energy thresholds for the relevant gates are set above the breathing signal at that distance; only the moving-target path is firing.
- **Occupancy never clears.** A gate covering a fan, a curtain over a vent, or the neighbouring room through drywall exceeds its threshold continuously, so the no-one timer never starts.
- **The parser reports nothing after enabling engineering mode.** Engineering-mode frames carry a different data-type byte and a different payload layout; a parser that accepts only the basic data type discards all of them.
- **A hallway sensor triggers on people who never enter.** Gates 7 and 8 extend to roughly 6 m and see through the doorway; the beam of about ±60° is wider than the intended zone.
- **Software serial produces corrupted frames.** The default rate is 256000 baud, above what bit-banged serial sustains reliably; a hardware UART is required.
- **Retuned thresholds survive a reflash unexpectedly.** Configuration persists in the module's flash, so a module tuned for a previous room carries those thresholds into the next deployment until they are rewritten.
- **Detection appears blocked at short range.** Metal in the beam path reflects rather than transmits; the module must not be mounted behind a metal plate.
