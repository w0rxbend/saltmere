---
title: "MicroPython on ESP32 in 2026: v1.28, Eight Chip Families, and a 30-Line Sensor Node"
date: 2026-08-13
track: iot-embedded
summary: "MicroPython v1.28.0 (April 2026) runs on essentially the whole ESP32 family — classic, S2, S3, C2, C3, C5, C6, P4. Where it earns a place next to ESP-IDF and Arduino in a sensor fleet, plus the path from esptool flash to an SHT4x publishing over MQTT with umqtt in about 30 lines."
reading_time: 6
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

**Gist.** Bringing up an unfamiliar sensor against ESP-IDF (Espressif IoT Development Framework) costs a compile-flash-monitor cycle for every hypothesis about the part's register map. MicroPython replaces that cycle with an interactive read-eval-print loop (REPL) running **on the target device**, so a bus scan and a register poke are two lines typed at a prompt rather than two rebuilds. The cost is a bytecode virtual machine (VM) between the script and the silicon: slower wake, larger flash and RAM footprint, and no access to the ESP-IDF-level controls that battery and transport-security work depend on.

## Port status

The current release is **v1.28.0 (April 2026)**. The esp32 port builds against a pinned ESP-IDF v5.x — the port's build instructions name the exact supported range, and building against an unlisted version is unsupported rather than merely untested. Board profiles cover the classic ESP32, S2, S3, C2, C3, C5 and C6, plus the P4; the newer families arrived later than the classic parts, so a given board's profile may postdate the silicon by several releases.

S3 firmware is published as **several variants per flash and SPIRAM (serial peripheral interface RAM, the external pseudo-static RAM attached over SPI) configuration**. The download page names the variant a given board needs; the generic image is not universal across S3 modules.

## Where the runtime fits

The distinction that matters is between rigs whose cost is *engineering time* and fleets whose cost is *energy and attack surface*.

**Suited:** sensor bring-up and prototyping, bench rigs, calibration jigs, one-off data-collection nodes, and any device that a Python-fluent, C-averse collaborator must maintain. The REPL makes an I2C (inter-integrated circuit) bus probe interactive: scan, write a command byte, read the response, decode by hand.

**Not suited:** the battery fleet. A MicroPython node **wakes more slowly** — the VM must boot and the script must be compiled — carries **the whole interpreter in flash and a resident heap in RAM** whether or not the script uses them — no published measurement separates the two footprints across comparable applications — and exposes none of the controls ESP-IDF does: TLS (transport layer security) session tickets, fine-grained Wi-Fi power-save tuning, ULP (ultra-low-power) coprocessor use. `machine.deepsleep()` functions correctly; across thousands of wake cycles the per-boot overhead is a measurable battery cost. Arduino occupies the middle position: C++ performance with friendlier application programming interfaces (APIs).

Two qualifications. **Performance:** interpreted bytecode is slow, but the `@micropython.native` and `@micropython.viper` decorators compile annotated functions to machine code, and tight bit-banging belongs in the C modules the firmware already ships — `machine.I2C`, `machine.bitstream`. At sensor cadences measured in seconds, the interpreter is not the bottleneck. **Security:** `umqtt.simple` accepts an `ssl` context, so TLS to the broker works, but **certificate-authority (CA) material is managed by hand and the full mbedTLS handshake is paid on every reconnect**, with none of the session-resumption control ESP-IDF offers. That is adequate on a lab network and thin on a hostile one.

## Flashing

Two tools, both installable with pip: `esptool` writes firmware, `mpremote` talks to the running board. The **application offset differs by chip family — 0 on S3, S2 and C3, and 0x1000 on the classic ESP32** — and each board's download page states the correct value. Writing an image at the wrong offset produces a board that resets in a loop with no usable REPL.

```bash
pip install esptool mpremote

esptool.py erase_flash
esptool.py --baud 460800 write_flash 0 ESP32_GENERIC_S3-20260406-v1.28.0.bin

mpremote                          # opens a REPL on the auto-detected port
mpremote mip install umqtt.simple # install the MQTT client onto the board
```

`mip` is MicroPython's on-device package installer. `mpremote` proxies it over the serial link through the host's network connection, so no Wi-Fi credentials are required on the board to install a library.

## A 30-line air node: SHT4x to MQTT

The SHT4x is the simplest Sensirion part to drive without a driver library: **write one command byte, wait, read six bytes**. The six bytes are temperature high, temperature low, cyclic redundancy check (CRC), humidity high, humidity low, CRC. Both 16-bit words are converted with the fixed affine transforms the part defines over the full 0–65535 range.

Saved as `main.py` and copied with `mpremote fs cp main.py :main.py`:

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

Three properties of this listing are load-bearing and three omissions are deliberate.

The **10 ms sleep between the command write and the read is not optional**: the high-precision command `0xfd` starts a conversion, and a read issued before it completes is not answered. The **clamp on relative humidity** is a consequence of the affine transform, whose range extends past the physically meaningful 0–100 % band. The **`keepalive=120`** argument sets the MQTT keepalive interval in seconds; `umqtt.simple` does not send pings on its own timer, so a broker will disconnect a node that stays inside the publish loop longer than the interval without traffic.

Omitted: **CRC-8 verification of bytes 2 and 5**, which is the same Sensirion algorithm covered in the SEN5x article; a **`try`/`except` around `publish` that reconnects on `OSError`**, without which one dropped TCP connection ends the loop; and **`machine.deepsleep(10_000)` in place of `time.sleep(10)`** for anything that leaves bench power. Note that `deepsleep` is not a substitute drop-in — it restarts the program from the top, so Wi-Fi association and the MQTT connect are re-paid each cycle, which is the same per-boot overhead that rules the runtime out of battery fleets.

## The development loop

For iteration, `main.py` can be skipped entirely: `mpremote run node.py` executes a host-side script against the live board without writing it to flash. When a node misbehaves, `mpremote` opens a REPL **on the misbehaving device**, where `read_sht4x()` can be called by hand and interpreter state inspected directly. The comparison is against inserting a print statement and reflashing.

The framing this supports: MicroPython is where firmware ideas become cheap to test. Ideas that survive get rewritten in C; many bench rigs never need to be.

## Pitfalls

- **Firmware written at the wrong offset boots into a reset loop.** The application offset is 0 on S3, S2 and C3 but 0x1000 on the classic ESP32; a single flash command reused across families produces an unbootable board with no REPL to diagnose it.
- **Reading the SHT4x before the conversion finishes returns no data.** The `0xfd` command needs its measurement interval; dropping the 10 ms sleep turns a working driver into an I2C read error.
- **Skipping the CRC bytes silently accepts corrupt readings.** Bytes 2 and 5 exist because the I2C line can glitch; ignoring them lets a bit-flipped word through as a plausible temperature.
- **An unguarded `publish` ends the loop on the first network hiccup.** `umqtt.simple` raises `OSError` on a broken socket and does not reconnect; without a handler the node stops publishing and looks dead.
- **A publish interval longer than the keepalive drops the connection.** `umqtt.simple` does not ping autonomously, so a node that sleeps past `keepalive` seconds without traffic is disconnected by the broker.
- **Replacing `time.sleep` with `machine.deepsleep` restarts the script from the top.** State held in module-level variables, the Wi-Fi association and the MQTT session do not survive; the code must be restructured, not one line edited.
- **TLS to the broker re-handshakes on every reconnect.** `umqtt.simple` takes an `ssl` context but gives no session-resumption control, so a flapping link pays the full mbedTLS handshake each time.
