---
title: "CAN Bus Toolhead Boards in Klipper: Katapult, One Cable, and a Debuggable Bus"
date: 2026-08-27
track: cad-3dprint
summary: "A toolhead board on a Controller Area Network (CAN) bus replaces the dozen-wire umbilical with two data wires plus power, carrying Klipper's microcontroller protocol over 8-byte frames at 1 Mbit/s. This article covers flashing the Katapult bootloader, the USB-adapter versus bridge topologies, the frame-level arithmetic that bounds stepper and accelerometer traffic, and the diagnostic path for termination faults, queue exhaustion, and 'Lost communication with MCU'."
reading_time: 8
tags: [klipper, can-bus, katapult, toolhead-board, firmware, debugging]
sources:
  - title: "Klipper documentation — CANBUS"
    url: "https://www.klipper3d.org/CANBUS.html"
  - title: "Klipper documentation — CANBUS Troubleshooting"
    url: "https://www.klipper3d.org/CANBUS_Troubleshooting.html"
  - title: "Katapult — configurable bootloader for ARM Cortex-M (GitHub)"
    url: "https://github.com/Arksine/katapult"
  - title: "Analog Devices — ADXL345 datasheet"
    url: "https://www.analog.com/media/en/technical-documentation/data-sheets/adxl345.pdf"
---

**Gist.** A direct-drive toolhead needs heater, thermistor, fan, stepper, probe and accelerometer connections, and routing each as a discrete wire through a moving drag chain multiplies flex-fatigue failure points. Mounting a small microcontroller board on the toolhead and running a Controller Area Network (CAN) bus back to the host collapses the umbilical to two data wires plus power, with Klipper treating the board as one more microcontroller (MCU) whose command stream is carried in 8-byte CAN frames. The costs are a fixed bus budget — roughly 9,000 full frames per second at 1 Mbit/s, of which an accelerometer stream alone can claim over a quarter — and a new class of bus-level failures that manifest as the generic "Lost communication with MCU" shutdown.

## Why a bus, and what Klipper puts on it

Klipper already splits work between a host (a Linux computer running `klippy`) and one or more MCUs that execute precisely timed events. The host compiles G-code into low-level commands — schedule a step pulse sequence, set a PWM value, read an ADC — and ships them to each MCU over a serial transport. CAN is one such transport: the same command protocol, framed into CAN 2.0 data frames carrying **at most 8 bytes of payload each**.

This division of labour is what makes a 1 Mbit/s bus sufficient for motion. The host does not send one message per step pulse; it sends **compressed step sequences** (an interval, a count, and an add term per block), and the toolhead MCU expands them into individual pulses locally. Bus traffic therefore scales with the *rate of change* of velocity — how often a new block must be issued — not with step frequency itself. A stepper executing 100,000 steps per second on the MCU may occupy only a small fraction of the bus, while the same motion streamed step-by-step would exceed the bus capacity outright.

Klipper's documentation specifies **1000000 (1 Mbit/s) as the recommended CAN bit rate**, and the rate is fixed at firmware compile time — for a bridge-mode adapter, the speed configured on the Linux interface is ignored. Every node on a bus must be compiled for the same rate; a single mismatched node corrupts arbitration for all of them.

## Two topologies

There are two ways to get the host onto the bus, and the choice affects both wiring and debuggability.

**Dedicated USB-to-CAN adapter.** A separate adapter (typically presenting the Linux `gs_usb` interface) sits on the bus as a pure translator. Every frame on the bus passes through it, so `candump` on the host sees all traffic between all nodes.

**USB-to-CAN bridge mode.** Many printer mainboards can be flashed with Klipper in *bridge* mode: the mainboard is simultaneously a Klipper MCU and a USB CAN adapter for the host. This saves a device and reuses the mainboard's transceiver. The documented limitation is subtle: **the bridge MCU is not itself observable on the bus** — its own exchange with the host happens over USB, so frames between host and bridge never appear to `candump` or to a logic analyzer clipped onto CANH/CANL. When the misbehaving node *is* the bridge, the bus capture shows nothing wrong.

In both topologies, discovery is the same. Each Klipper CAN node derives a persistent 12-hex-digit identifier, queried with:

```sh
~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0
```

and referenced in the printer configuration as `canbus_uuid` under a second `[mcu]` section. A node answers the query only while unassigned; a board already claimed by a running Klipper instance does not reappear, which routinely misleads first-time setups into re-flashing a working board.

## Katapult: flashing without touching the toolhead

The bootstrapping problem: the toolhead board hangs off two wires, so reflashing Klipper after every update must work over those wires. **Katapult** (formerly CanBoot) is a bootloader for ARM Cortex-M parts — stm32, rp2040, lpc176x — built on a stripped-down copy of Klipper's hardware abstraction layer, whose job is to accept an application image over CAN, USB or UART.

The sequence is flashed once by wire, then never again:

1. Build Katapult (`make menuconfig && make`) selecting the MCU, the CAN interface and pins, and **the same bit rate as the rest of the bus**. Flash it over USB device firmware upgrade (DFU) or a debug probe — the only step requiring physical access.
2. Build Klipper for the same MCU and bit rate, with an application start offset matching Katapult's reserved flash region.
3. Upload over the bus:

```sh
python3 ~/katapult/scripts/flashtool.py -i can0 \
    -f ~/klipper/out/klipper.bin -u <canbus_uuid>
```

Katapult runs at reset and jumps to the application; three documented paths re-enter it: **an empty application flash region** (automatic), **a double-press of reset within 500 ms** (if enabled in menuconfig), or a **configured GPIO button**. `flashtool.py -r` requests bootloader entry over the bus from a running Klipper node, which is what makes remote re-flashing routine. The failure to avoid is bricking-by-mismatch: flashing a Klipper image built for a different bit rate leaves the node mute, and recovery then depends on the double-press or GPIO entry path having been enabled.

## The bus budget

The arithmetic is worth doing once, because it bounds every "can the bus handle X" question. A CAN 2.0 data frame with an 11-bit identifier and 8 data bytes contains 108 bits of structure (start-of-frame, identifier, control, 64 data bits, 15-bit cyclic redundancy check, acknowledge and end-of-frame), plus a 3-bit interframe space: **111 bits nominal per 8-byte frame**. CAN also inserts a stuff bit after any five consecutive equal bits between start-of-frame and the CRC; over that 98-bit stuffable region the worst case adds 24 bits, so a frame occupies **111–135 bit times** depending on payload contents.

At 1 Mbit/s that is 111–135 µs per frame — a ceiling of roughly **7,400–9,000 full frames per second**, or about 59–72 KB/s of payload, before protocol overhead within Klipper's own messages.

Now place the toolhead's loads against that ceiling:

- **Accelerometer streaming.** An ADXL345 at its maximum output data rate of **3200 Hz** produces 3 axes × 2 bytes = 6 bytes per sample, 19,200 B/s of raw payload. Packed into 8-byte frames that is a floor of 2,400 frames/s — **over a quarter of the bus** — before Klipper's message framing, clock sync, and acknowledgements. This is why resonance measurement is the canonical trigger for CAN overruns: printing plus streaming can exceed what an undersized transmit queue absorbs.
- **Stepper blocks.** Smooth constant-velocity motion is cheap; what is expensive is **high junction-deviation, high-acceleration motion with input shaping**, where velocity changes force frequent new step blocks. The load is bursty, and bursts are exactly what shallow queues drop.
- **Everything else** — heater PWM, thermistor ADC readings at a few hertz, fan control — is noise by comparison, tens of frames per second.

The Linux side has a matching knob: the default transmit queue on a CAN interface is 10 frames, and Klipper's troubleshooting guide recommends **`txqueuelen 128`**. Exhausting the queue surfaces in the log as `Got error -1 in can write: (105)No buffer space available` — a host-side symptom frequently misread as a wiring fault.

## Diagnosing the classic failures

CAN faults compress into one user-visible error — "Lost communication with MCU" — so diagnosis means working down the stack.

**Termination.** A CAN bus is a transmission line requiring **exactly two 120 Ω terminating resistors, one at each physical end**. The static check is a multimeter across CANH/CANL with power removed: **approximately 60 Ω** is correct. 120 Ω means one terminator (often works on a short bench bus, fails sporadically at length); 40 Ω means three (a common outcome when both the adapter and mainboard ship with jumpered terminators and the toolhead board adds a third). Under-termination produces reflections that corrupt frames intermittently — the worst kind of fault, correlating with nothing.

**Bit-rate and timing mismatch.** A node compiled at the wrong rate does not merely stay silent; its error frames can disturb the whole bus. The symptom is a node that never answers `canbus_query.py` while the bus statistics accumulate errors. Since the rate is baked in at compile time on the firmware side, the fix is a rebuild, not a Linux setting.

**Software-level corruption.** Klipper logs per-second statistics including a `bytes_invalid` counter. The troubleshooting guide attributes an **incrementing `bytes_invalid`** to message reordering from outdated software on the host or adapter — a firmware/kernel update problem, not a wiring one. A stable counter with dropped connections points back at the physical layer.

**Raw capture.** When the layers above disagree, `candump -tz -Ddex can0,#FFFFFFFF` records every frame with timestamps, and Klipper ships `parsecandump.py` to decode its MCU protocol from that capture. The bus is, by construction, observable — with the bridge-mode caveat above: traffic between the host and the bridge MCU itself never crosses the wires being probed.

## Pitfalls

- **Reading ~40 Ω across CANH/CANL and proceeding anyway** — three terminators from stacked default jumpers; frames corrupt intermittently and only under load.
- **Flashing Klipper at a different bit rate than Katapult and the bus** — the node goes mute, and recovery requires the double-press or GPIO bootloader entry that menuconfig left disabled.
- **Expecting `canbus_query.py` to list a board already claimed by Klipper** — assigned nodes do not answer the query, and the "missing" board gets needlessly re-flashed.
- **Leaving `txqueuelen` at the default 10** — accelerometer streaming during a print exhausts the transmit queue and Klipper aborts with `(105)No buffer space available`.
- **Probing the bus to debug a bridge-mode mainboard** — the bridge's own traffic runs over USB, so `candump` shows a healthy bus while the failing link is elsewhere.
- **Trusting the Linux interface bit-rate setting in bridge mode** — Klipper ignores it; the effective rate is whatever the firmware was compiled with.
