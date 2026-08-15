---
title: "Arduino Uno Q and Arduino Under Qualcomm: What Changed for Makers"
date: 2026-08-15
track: iot-embedded
summary: "Qualcomm announced its acquisition of Arduino in October 2025 and led with a new flagship: the UNO Q, a 'dual-brain' board pairing a Dragonwing QRB2210 Linux SoC with an STM32U585 MCU in the classic UNO footprint. Ten months on, here's what's real — the hardware, the App Lab workflow and its MessagePack-RPC bridge, the July 2026 price hike, the terms-of-service backlash — and how it stacks up against a Raspberry Pi 5 plus a Pico."
reading_time: 5
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

In October 2025, Qualcomm announced it was acquiring Arduino — the company behind the board most of us learned on — and on the same day Arduino launched the **UNO Q**, a board that is very deliberately not just another UNO. Ten months later the dust has settled enough to assess what actually changed for people who build things.

## The hardware: two brains in an UNO footprint

The UNO Q's pitch is "dual brain": a Linux application processor and a real-time MCU on one board, in the classic UNO form factor with shield headers and a Qwiic connector.

| | Linux side | MCU side |
|---|---|---|
| Chip | **Qualcomm Dragonwing QRB2210** | **STM32U585** |
| Cores | 4× Cortex-A53 @ 2.0 GHz + Adreno GPU | Cortex-M33 @ 160 MHz |
| Memory | 2 GB/16 GB or 4 GB/32 GB eMMC | 2 MB flash, 786 KB SRAM |
| Runs | Debian Linux | Arduino sketches (Zephyr-based core) |

Radio is Wi-Fi 5 plus Bluetooth 5.1, and the bottom carries high-speed connectors for MIPI-CSI cameras and a MIPI-DSI display — the QRB2210's ISP can handle 13 MP cameras, which is the "AI vision" angle. Storage is soldered eMMC, not an SD card: less flexible, but it boots fast and won't corrupt the way SD cards do.

Pricing is the sore point. The board launched at **$44 (2 GB) / $59 (4 GB)** — aggressive, clearly aimed at Raspberry Pi. In June 2026 Arduino announced that memory costs (more than doubled by AI-driven DRAM demand) forced a hike to **$59 / $79** from July 6. Still competitive, but the headline "Pi-killer at $44" era lasted about eight months.

## App Lab and the bridge: the actual new idea

The genuinely new part isn't the silicon, it's the workflow. **Arduino App Lab** treats an "app" as one project containing a Python program (Linux side), an Arduino sketch (MCU side), and optionally an AI model, deployed together. The two sides talk over **MessagePack-RPC** through a router service — the sketch registers functions, Python calls them by name (and the reverse works too):

```cpp
// MCU side (sketch): expose a function the Linux side can call
#include "Arduino_RouterBridge.h"

void set_led(bool on) { digitalWrite(LED_BUILTIN, on); }

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  Bridge.provide_safe("set_led", set_led);   // register before begin()
  Bridge.begin();
}
void loop() {}
```

```python
# Linux side (Python): call into the MCU
from arduino.app_utils import *
import time

def loop():
    Bridge.call("set_led", True);  time.sleep(0.5)
    Bridge.call("set_led", False); time.sleep(0.5)

App.run(user_loop=loop)
```

If you've ever duct-taped a Pi to a microcontroller with pyserial and a hand-rolled framing protocol, this is that — done properly, with one IDE, one deploy step, and typed RPC instead of `Serial.parseInt()`. That's the real answer to "why not a Pi 5 and a Pico?" A Pi 5 has far more compute, a vastly bigger ecosystem, and swappable storage; a Pico is a more than capable MCU; together they're cheaper than $79. But you own the integration: two boards, a cable or UART link, your own protocol, your own provisioning. The UNO Q makes the hybrid architecture a first-class, single-board target — and keeps shield compatibility for the drawer of hardware you already have.

## The Qualcomm question

The community's reaction to the acquisition itself was wary but calm — Qualcomm committed to keeping the open-source model and the existing product line. The flashpoint came a month later, when Arduino published **rewritten Terms of Service**. Adafruit's widely shared analysis flagged perpetual licenses on user-uploaded content, AI-related data monitoring, and reverse-engineering restrictions, reading it as Arduino shifting "from an open community platform into a tightly controlled corporate service." Arduino responded quickly and in writing: **"anything that was open, stays open"** — hardware remains open-source, reverse engineering your own devices stays legitimate, users keep ownership of their creations, and the legal language was revised.

Where does that leave a maker in mid-2026? The IDE, cores, and hardware design files remain open; UNO Q schematics are published, and the sketches-plus-Python model works offline. The legitimate open questions are structural: App Lab's nicest flows nudge you toward Arduino Cloud accounts; the QRB2210's GPU/ISP stack involves the usual Qualcomm proprietary blobs (the Debian image handles this, but it's not a fully mainline experience); and long-term product decisions now serve a chip company's edge-AI strategy, not only the classroom. None of that is disqualifying. All of it is worth watching with the same skepticism the ToS episode earned.

**Verdict:** the UNO Q is a genuinely good board for the specific shape of project where one box needs both a camera-and-Python brain and hard-real-time pins — robots, smart instruments, vision-plus-actuation. For headless sensor fleets, an ESP32 is still an order of magnitude cheaper and sleeps in microamps; for desktop-class Linux, a Pi 5 still wins.

**Try next:** prototype the split-brain pattern for free on hardware you own — put your existing Pi and any MCU on a MessagePack-RPC link and see how much of your "serial glue" code disappears; then you'll know whether the UNO Q's integrated version is worth $59 to you.
