---
title: "MicroPython on ESP32 in 2026: v1.28, Eight Chip Families, and a 30-Line Sensor Node"
date: 2026-08-13
track: iot-embedded
summary: "MicroPython v1.28.0 (April 2026) runs on essentially the whole ESP32 family now — classic, S2, S3, C2, C3, C5, C6, P4. Where it earns a place next to ESP-IDF and Arduino in a sensor fleet, plus the full path from esptool flash to an SHT4x publishing over MQTT with umqtt in about 30 lines."
reading_time: 5
tags: [micropython, esp32, mqtt, i2c, prototyping]
sources:
  - title: "MicroPython downloads — ESP32-S3 firmware"
    url: "https://micropython.org/download/ESP32_GENERIC_S3/"
  - title: "MicroPython v1.28.0 release notes — GitHub"
    url: "https://github.com/micropython/micropython/releases"
  - title: "Getting started with MicroPython on the ESP32 — MicroPython docs"
    url: "https://docs.micropython.org/en/latest/esp32/tutorial/intro.html"
  - title: "mpremote — MicroPython remote control — MicroPython docs"
    url: "https://docs.micropython.org/en/latest/reference/mpremote.html"
  - title: "MicroPython vs. Arduino: Which one should you choose in 2026? — Soldered"
    url: "https://soldered.com/blogs/learn/micropython-vs-arduino"
---

I write most of this journal in C against ESP-IDF, so it may surprise you that half my sensor bring-up happens in MicroPython first. In 2026 that's a more defensible position than ever: the port matrix caught up with Espressif's silicon, and the tooling matured into something you can hand a colleague.

## Where MicroPython stands in 2026

Current release is **v1.28.0, out 6 April 2026**. The esp32 port now builds against ESP-IDF v5.3–v5.5.1 and covers essentially the whole family: classic ESP32, S2, S3, C2, C3, C5, and C6, plus the P4 — the C5 and P4 gained board profiles in v1.27.0 (December 2025), and v1.28 added boards like the SparkFun Thing Plus ESP32-C5 and Seeed XIAO ESP32-C6. Headline language feature this cycle: PEP 750 template strings and nested f-strings, which is more CPython compatibility than you might expect from a microcontroller runtime. Firmware images for S3 want 4 MiB+ flash and auto-detect SPIRAM.

## When I reach for it (and when I don't)

**Reach for it:** sensor bring-up and prototyping. An interactive REPL on the device means I can probe an I2C bus, poke registers, and have a new sensor decoded in minutes — no compile-flash-monitor loop. Also: bench rigs, calibration jigs, one-off data-collection nodes, and anything a Python-fluent, C-shy collaborator needs to maintain.

**Don't:** the battery fleet. A MicroPython node wakes slower (VM boot plus script compile), eats a few hundred KB more flash and tens of KB more RAM, and gives you no access to the knobs I lean on in ESP-IDF — TLS session tickets, fine Wi-Fi power-save tuning, ULP coprocessor tricks. `machine.deepsleep()` works fine, but at thousands of wake cycles the boot overhead is real battery. Arduino sits in the middle: C++ performance with friendlier APIs, covered in the core 3.x article.

Two nuances worth knowing before you commit. Performance: interpreted bytecode is slow, but the `@micropython.native` and `@micropython.viper` decorators compile hot functions to machine code, and tight bit-banging belongs in the C modules the firmware already ships (`machine.I2C`, `machine.bitstream`) — so "Python is slow" rarely bites at sensor cadences of seconds. Security: `umqtt.simple` accepts an `ssl` context, so TLS to the broker works, but you're managing CA certificates by hand and paying the mbedTLS handshake on every reconnect with none of the session-resumption control ESP-IDF gives you — fine for a lab, thin for a hostile network.

The honest framing: MicroPython is where firmware ideas get cheap to test. Winners get rewritten in C; plenty of rigs never need to be.

## Flashing it

Two tools, both pip-installable: `esptool` to flash firmware, `mpremote` to talk to the running board. For an S3 (note: app offset is 0 on S3/S2/C3; the classic ESP32 uses 0x1000 — the download page for each board states it):

```bash
pip install esptool mpremote

esptool.py erase_flash
esptool.py --baud 460800 write_flash 0 ESP32_GENERIC_S3-20260406-v1.28.0.bin

mpremote                          # opens a REPL on the auto-detected port
mpremote mip install umqtt.simple # install the MQTT client onto the board
```

`mip` is MicroPython's on-device package installer, and `mpremote` proxies it through your PC's network connection — no Wi-Fi setup needed just to install libraries.

## A 30-line air node: SHT4x → MQTT

The SHT4x is the easiest Sensirion part to drive raw: one command byte, wait, read six bytes. No driver library needed. Save as `main.py` and copy with `mpremote fs cp main.py :main.py`:

```python
import time, network, machine
from umqtt.simple import MQTTClient

SHT4X = 0x44
i2c = machine.I2C(0, scl=machine.Pin(9), sda=machine.Pin(8))

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect("saltmere-iot", "wifi-password")
while not wlan.isconnected():
    time.sleep_ms(200)

def read_sht4x():
    i2c.writeto(SHT4X, b"\xfd")        # high-precision measurement
    time.sleep_ms(10)
    d = i2c.readfrom(SHT4X, 6)         # t_hi t_lo crc rh_hi rh_lo crc
    t = -45 + 175 * ((d[0] << 8) | d[1]) / 65535
    rh = -6 + 125 * ((d[3] << 8) | d[4]) / 65535
    return t, min(100, max(0, rh))

c = MQTTClient("mp-node-01", "192.168.1.10", keepalive=120)
c.connect()
while True:
    t, rh = read_sht4x()
    c.publish(b"saltmere/lab/mp-node-01",
              b'{"t_c":%.2f,"rh":%.1f}' % (t, rh))
    time.sleep(10)
```

That's the entire node. Things I'd add before trusting it: CRC-8 checks on bytes 2 and 5 (I covered the Sensirion CRC algorithm in the SEN5x article — it's identical here), a try/except around `publish` that reconnects on `OSError`, and `machine.deepsleep(10_000)` instead of `time.sleep(10)` if it ever leaves the bench. For development, skip `main.py` entirely and iterate with `mpremote run node.py` — the script executes from your PC against the live board, which is the fastest firmware feedback loop that exists on this hardware.

The debugging experience deserves a sentence: when the node misbehaves, `mpremote` drops you into a REPL *on the misbehaving device*, where you can call `read_sht4x()` by hand and inspect state. Compare that to adding printf and reflashing, and you understand why prototypes start here.

**Try next:** flash v1.28.0 on a spare S3 board, then run `mpremote` and scan your I2C bus interactively with `machine.I2C(0, scl=machine.Pin(9), sda=machine.Pin(8)).scan()` — whatever addresses come back, you're three REPL lines from reading the part.
