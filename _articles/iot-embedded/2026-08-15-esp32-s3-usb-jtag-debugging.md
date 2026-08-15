---
title: "Debugging an ESP32-S3 Over Its Built-in USB-JTAG: GDB, OpenOCD, and Breakpoints in a Sensor Loop"
date: 2026-08-15
track: iot-embedded
summary: "The ESP32-S3 carries a USB-Serial-JTAG peripheral on-die — no FT2232 probe, no wiring beyond the USB D+/D- pins on GPIO19/GPIO20. This describes the idf.py openocd + idf.py gdb workflow on ESP-IDF v6.0.2: halting a running sensor loop on a hardware breakpoint, watching an I2C variable change, and the constraints — 2 hardware breakpoints on the S3, and flash encryption that disables JTAG entirely."
reading_time: 6
tags: [esp32, esp32-s3, jtag, openocd, gdb, debugging]
sources:
  - title: "JTAG Debugging — ESP-IDF Programming Guide v6.0.2 (ESP32-S3)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/jtag-debugging/index.html"
  - title: "Configure the ESP32-S3 built-in JTAG Interface — ESP-IDF v6.0.2"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/jtag-debugging/configure-builtin-jtag.html"
  - title: "JTAG Debugging: Tips and Quirks — ESP-IDF v6.0.2 (ESP32-S3)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/jtag-debugging/tips-and-quirks.html"
  - title: "Using Debugger — ESP-IDF Programming Guide v6.0.2 (ESP32-S3)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-guides/jtag-debugging/using-debugger.html"
---

**Gist.** Halting a live firmware task to inspect its state has historically required an external Joint Test Action Group (JTAG) probe — an FT2232H adapter and the wired JTAG signals (TMS, TCK, TDI, TDO, plus ground). The ESP32-S3 integrates a **USB-Serial-JTAG** controller on-die, so the same USB cable used for flashing presents a JTAG adapter to OpenOCD and, through it, to GDB. The cost is paid in scarce on-chip debug resources — **2 hardware breakpoints and 2 watchpoints** — and in mutual exclusion with the security fuses and with any other use of the two pins the peripheral owns.

## The interface is the USB port

The S3's USB-Serial-JTAG occupies two fixed pins: **GPIO19 is USB D−** and **GPIO20 is USB D+**, alongside 5 V on V_BUS and ground. Any board whose USB connector is wired to those pins — the case on S3 devkits — needs no further hardware. A bare module requires a USB breakout on those four lines and nothing more. On Linux the device needs a udev rule; on Windows the port must be bound to the WinUSB driver, which the Espressif tooling or Zadig performs.

The consequence of fixed pins is exclusivity. **GPIO19 and GPIO20 are the JTAG bus while debugging.** Firmware that reconfigures those pins for another function, or that drives the native USB On-The-Go (OTG) stack through TinyUSB, contends for the same pads; one role per pin at a time.

## Launching OpenOCD and GDB

ESP-IDF wraps the toolchain behind `idf.py`, so raw OpenOCD invocations are rarely needed. On **ESP-IDF v6.0.2**, the conventional arrangement is two terminals — OpenOCD as the server holding the JTAG transport, GDB as the client speaking the GDB remote serial protocol to it:

```bash
# Terminal 1 — on-chip debug server, built-in JTAG configuration.
idf.py openocd
# equivalent to: openocd -f board/esp32s3-builtin.cfg

# Terminal 2 — GDB, pointed at the project's ELF, connected to OpenOCD.
idf.py gdb
```

`idf.py openocd` selects the `esp32s3-builtin.cfg` board file automatically, so no interface argument is required. `idf.py gdb` loads the built ELF and a FreeRTOS-aware Python extension, which is what makes per-task backtraces legible rather than a single undifferentiated stack. The two actions may be chained on one line, because `idf.py` orders background actions (OpenOCD) before interactive ones (GDB):

```bash
idf.py openocd gdb
```

Two frontends exist over the same session: `idf.py gdbtui` provides a split source view, and `idf.py gdbgui` opens a browser-based debugger.

## Halting inside a running sensor loop

Consider a particulate-matter (PM2.5) task that reads a sample each second and intermittently yields an implausible value. The debug session halts inside the read path and inspects the sample in place:

```gdb
(gdb) break pm25_task.c:88
Breakpoint 1 at 0x42012a4c: file pm25_task.c, line 88.
(gdb) continue
Continuing.

Breakpoint 1, hnd_read_sample () at pm25_task.c:88
88          esp_err_t err = sps30_read(&raw);
(gdb) next
(gdb) print raw.pm2p5
$1 = 12.4000006
(gdb) watch raw.pm2p5          # halt when the variable changes
Hardware watchpoint 2: raw.pm2p5
(gdb) continue
Hardware watchpoint 2: raw.pm2p5
Old value = 12.4000006
New value = 998.200012          # the anomalous sample
(gdb) backtrace
```

The load-bearing instruction is `watch`. It installs a **hardware watchpoint**: the core traps on the write to that address, so the halt occurs at the instruction that produced the value rather than at the next poll of it. The backtrace taken at that halt therefore names the actual writer — a driver path, a shared buffer, or an unrelated task overrunning its allocation — which polling or added logging cannot distinguish, because both observe the value only after the fact. `print` reads any in-scope variable or structure directly from the halted core, no target-side formatting code involved.

## Debug resources are a fixed, small budget

The controlling constraint is unit count, not speed. The ESP32-S3 provides **2 hardware breakpoints and 2 watchpoints**. A request for a third of either fails at the point GDB attempts to install it, which is on `continue` rather than at the `break` command, so an over-subscribed session appears to accept the breakpoint and then errors when resumed.

Breakpoints have an escape hatch that watchpoints do not. Up to **64 software breakpoints** are available in flash and internal RAM (IRAM); a software breakpoint is an instruction rewritten in place, so its supply is bounded by memory rather than by comparator hardware. Watchpoints have no software equivalent here — a data write cannot be trapped without hardware address comparison — so **two is the hard ceiling on simultaneously watched variables**.

Other Espressif parts do not all carry the same budget; the per-chip counts are stated in each chip's own version of the tips-and-quirks page, and a design that must observe several variables concurrently is therefore affected by chip selection.

## Interaction with the security fuses

**Enabling Flash Encryption and/or Secure Boot disables JTAG debugging.** The peripheral is fused off, which makes the transition one-way on a given part: a node prepared for production is no longer reachable by the workflow above. `CONFIG_SECURE_BOOT_ALLOW_JTAG` keeps JTAG operational with Secure Boot enabled, at the cost of the physical-access protection those features provide, which confines it to development units.

A second interaction is subtler and follows from what a software breakpoint is. Because it rewrites an instruction in flash, and Secure Boot validates the application image against its signature, **a software breakpoint can invalidate the app signature on the next reset**. Under a Secure Boot build the two hardware breakpoints are the ones that leave the image intact.

## Pitfalls

- Firmware that claims GPIO19/GPIO20 — for general-purpose I/O or for the native USB-OTG stack via TinyUSB — takes the pins away from USB-Serial-JTAG; the symptom is a debug session that cannot attach or drops once that firmware initialises, because the peripheral and the reassigned function share the same pads.
- A third hardware breakpoint or a third watchpoint is refused when GDB installs it on resume, not when the command is typed; the symptom is an error on `continue` that appears unrelated to the last command entered.
- Watchpoints have no software fallback: exhausting the two on an S3 cannot be worked around by trading them for software breakpoints, since only breakpoints have a software form.
- Enabling Flash Encryption or Secure Boot fuses JTAG off, so a board that debugged correctly before provisioning stops responding to OpenOCD afterwards, with no firmware change to explain it.
- On a Secure Boot build, a software breakpoint modifies an instruction in flash; the symptom is a boot failure after reset, caused by the application signature no longer matching the modified image.
- On Windows an unbound port presents as an enumerated device that OpenOCD will not open; the cause is the missing WinUSB driver binding rather than a wiring or configuration fault.
