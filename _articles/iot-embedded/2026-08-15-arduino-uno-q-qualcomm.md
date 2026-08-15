---
title: "Arduino UNO Q Under Qualcomm: Hardware, Bridge, and Terms"
date: 2026-08-15
track: iot-embedded
summary: "Qualcomm announced its acquisition of Arduino in October 2025 alongside the UNO Q, a 'dual-brain' board pairing a Dragonwing QRB2210 Linux system-on-chip with an STM32U585 microcontroller in the UNO footprint. Ten months on: the hardware, the App Lab workflow and its MessagePack-RPC bridge, the July 2026 price increase, the terms-of-service revision, and the comparison against a Raspberry Pi 5 paired with a Pico."
reading_time: 6
tags: [arduino, uno-q, qualcomm, stm32, linux, sbc, maker]
sources:
  - title: "Arduino UNO Q — official documentation"
    url: "https://docs.arduino.cc/hardware/uno-q"
  - title: "Arduino joins the Qualcomm family — announcement"
    url: "https://www.arduino.cc/qualcomm"
  - title: "A heads-up on Arduino UNO Q board pricing — Arduino Blog (June 2026)"
    url: "https://blog.arduino.cc/2026/06/26/a-heads-up-on-the-arduino-uno-q-board-pricing-straight-from-marcello-majonchi/"
  - title: "Arduino clarifies Terms and Conditions following backlash — Hackster.io"
    url: "https://www.hackster.io/news/arduino-clarifies-terms-and-conditions-following-backlash-anything-that-was-open-stays-open-645e9ee9a51e"
  - title: "Arduino Uno Q review — Tom's Hardware"
    url: "https://www.tomshardware.com/raspberry-pi/arduino-uno-q-review"
---

**Gist.** Projects that need both a Linux-class application processor (for cameras, Python, and machine-learning models) and a microcontroller unit (MCU) with deterministic pin timing have conventionally been built from two boards joined by a hand-written serial protocol. The Arduino UNO Q places both processors on one UNO-footprint board and supplies a remote procedure call (RPC) layer over MessagePack so the two sides exchange named function calls instead of parsed serial text. The cost is a fixed, soldered configuration — one system-on-chip (SoC), one MCU, non-removable storage — at a price that rose from $44 to $59 for the entry variant in July 2026, and a platform whose roadmap now sits inside a silicon vendor.

## The hardware: two processors in an UNO footprint

The board carries a Linux application processor and a real-time MCU behind the classic UNO shield headers, plus a Qwiic connector.

| | Linux side | MCU side |
|---|---|---|
| Chip | **Qualcomm Dragonwing QRB2210** | **STM32U585** |
| Cores | 4× Cortex-A53 @ 2.0 GHz + Adreno GPU | Cortex-M33 @ 160 MHz |
| Memory | 2 GB/16 GB or 4 GB/32 GB eMMC | 2 MB flash, 786 KB SRAM |
| Runs | Debian Linux | Arduino sketches (Zephyr-based core) |

Radio support is Wi-Fi 5 and Bluetooth 5.1. The underside carries high-speed connectors for a MIPI-CSI camera and a MIPI-DSI display; **the QRB2210 image signal processor accepts sensors up to 13 megapixels**, which is the basis of the board's vision positioning.

Storage is **soldered embedded MultiMediaCard (eMMC), not a removable SD card**. The practical consequence is asymmetric: soldered storage removes the mechanical failure modes of a card in a socket — contact wear, vibration, accidental ejection — and it removes the recovery path in which a corrupted card is reimaged on another machine. A bricked root filesystem is repaired over the board's own interfaces or not at all.

The division of labour is the point of the architecture. The Cortex-A53 cluster runs a general-purpose scheduler under Linux, so any latency guarantee it offers is statistical; the Cortex-M33 executes a single sketch with interrupt latency bounded by the hardware. Work whose deadline is measured in microseconds — step generation, pulse-width capture, bit-banged protocols — belongs on the MCU regardless of how much headroom the A53 cores appear to have.

Pricing is the contested part. The board launched at **$44 (2 GB) / $59 (4 GB)**. In June 2026 Arduino stated that memory costs, driven by demand for dynamic random-access memory (DRAM) in artificial-intelligence systems, had more than doubled, and announced an increase to **$59 / $79 effective 6 July 2026**.

## App Lab and the bridge

The novel element is the workflow rather than the silicon. **Arduino App Lab** defines an "app" as a single project containing a Python program for the Linux side, an Arduino sketch for the MCU side, and optionally a machine-learning model, deployed together as one unit.

The two sides communicate by **MessagePack-RPC through a router service**. MessagePack is a binary serialisation format; MessagePack-RPC layers request/response semantics on top of it, so a call carries a function name and typed arguments rather than a line of text that the receiver must re-parse. The sketch registers named functions; the Python program invokes them by name, and the direction reverses equally.

A minimal pair of endpoints:

```cpp
// MCU side (sketch): expose a function the Linux side can call
#include <Arduino_RouterBridge.h>

void set_led(bool on) { digitalWrite(LED_BUILTIN, on); }

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Bridge.begin();
  Bridge.provide("set_led", set_led);   // bound to the name, not the symbol
}
void loop() {}
```

```python
# Linux side (Python): call into the MCU
from arduino.app_utils import *
import time

def loop():
    Bridge.call("set_led", True)
    time.sleep(0.5)
    Bridge.call("set_led", False)
    time.sleep(0.5)

App.run(user_loop=loop)
```

**The coupling between the two programs is a string, checked at run time.** The sketch and the Python program are compiled and interpreted separately, so a name that is misspelled on one side, or never registered at all, produces no compile error and no boot failure: the sketch links and runs, and the fault appears as a failed call at the moment the peer first invokes that name. Argument types are likewise resolved by the serialisation format rather than by a shared header.

The comparison against a Raspberry Pi 5 plus a Raspberry Pi Pico remains open on the merits. The Pi 5 offers substantially more compute, a larger software ecosystem, and removable storage; the Pico is a capable MCU; the pair costs less than $79. What the pair does not supply is the integration: two boards, a physical link, a framing and error-handling protocol written by hand, and two separate provisioning paths. The UNO Q makes the split-processor architecture a single deployable target while retaining shield compatibility.

## The Qualcomm question

Reaction to the acquisition was cautious rather than hostile; Qualcomm stated a commitment to the open-source model and the existing product line. The dispute arrived with **rewritten Terms of Service**. Adafruit's analysis identified perpetual licences over user-uploaded content, data monitoring related to artificial intelligence, and reverse-engineering restrictions, characterising the change as a shift "from an open community platform into a tightly controlled corporate service." Arduino responded in writing that **"anything that was open, stays open"**: hardware remains open source, reverse engineering of one's own devices remains legitimate, users retain ownership of their creations, and the legal text was revised.

The position as of mid-2026: the integrated development environment, the cores, and the hardware design files remain open; UNO Q schematics are published; the sketch-plus-Python model functions offline. Three structural questions remain open rather than resolved. App Lab's smoothest workflows route through Arduino Cloud accounts. The QRB2210 graphics and image-signal stack depends on Qualcomm proprietary binaries — the supplied Debian image incorporates them, so this is not a mainline-kernel experience. And product direction is now set inside a silicon vendor pursuing an edge-AI strategy.

The board suits projects that require a camera-and-Python processor and hard-real-time pins inside one enclosure: robots, instrumentation, vision combined with actuation. For headless sensor deployments an ESP32 costs an order of magnitude less and sleeps at microampere currents; for desktop-class Linux the Pi 5 remains the stronger choice.

## Pitfalls

- **Mistyping a bridge function name on either side** produces no compile error and no boot failure; the peer's first call to that name finds no handler, so the fault surfaces as a runtime RPC error far from its cause.
- **Placing microsecond-deadline work on the Linux side** yields timing that is statistically acceptable and occasionally catastrophic: the Cortex-A53 cluster runs a general-purpose scheduler, so jitter is unbounded in the worst case even when average latency looks adequate.
- **Assuming SD-card recovery habits transfer** fails on soldered eMMC. A root filesystem corrupted beyond boot cannot be pulled, imaged, and reinserted on another machine.
- **Budgeting from launch prices** understates cost by $15–$20 per board: the entry configuration moved from $44 to $59 and the 4 GB configuration from $59 to $79 on 6 July 2026.
- **Treating the Debian image as a stock distribution** breaks when the graphics and image-signal paths are exercised, because those depend on Qualcomm proprietary binaries shipped with the image rather than on mainline kernel drivers.
- **Reading "anything that was open, stays open" as covering the whole stack** overstates it. The statement addresses hardware openness, reverse engineering, and content ownership; it is not a claim that the QRB2210 multimedia stack is free of proprietary components.
