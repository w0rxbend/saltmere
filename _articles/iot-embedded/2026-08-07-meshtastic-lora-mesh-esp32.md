---
title: "Meshtastic: an off-grid encrypted LoRa mesh on ESP32 hardware"
date: 2026-08-07
track: iot-embedded
summary: "Meshtastic replaces the LoRaWAN gateway and network server with managed flooding between the nodes themselves: every board rebroadcasts packets it has not seen, bounded by a hop counter defaulting to 3. The result is an encrypted text-and-telemetry mesh on commodity ESP32 and nRF52 boards, at the cost of lower capacity and no cloud backend."
reading_time: 6
tags: [meshtastic, lora, mesh, esp32, off-grid]
sources:
  - title: "Meshtastic — official documentation (Getting Started, flashing, LoRa region, channels)"
    url: "https://meshtastic.org/docs/"
  - title: "meshtastic/firmware — official firmware releases (GitHub)"
    url: "https://github.com/meshtastic/firmware/releases"
  - title: "Meshtastic Python CLI Guide"
    url: "https://meshtastic.org/docs/software/python/cli/"
  - title: "Telemetry Module — Meshtastic configuration docs"
    url: "https://meshtastic.org/docs/configuration/module/telemetry/"
  - title: "LoRaWAN vs. Meshtastic: Choosing the Right Network (Seeed Studio, June 2026)"
    url: "https://www.seeedstudio.com/blog/2026/06/11/lorawan-vs-meshtastic-choosing-the-right-network-for-your-project/"
---

**Gist.** Long Range (LoRa) radio reaches kilometres, but LoRaWAN spends that range on a star topology that is inert without a gateway and a network server. Meshtastic removes both: the nodes are the network, each one rebroadcasting packets it has not previously seen under a decrementing hop counter, so a message walks device-to-device with no infrastructure. The cost is a chattier, lower-capacity link than a LoRaWAN star, no backend unless an MQTT bridge is added, and a configuration surface — region, modem preset, channel key — where any mismatch renders two radios mutually deaf.

## Mesh topology versus the LoRaWAN star

LoRaWAN is a **star topology**. End devices transmit only to gateways; gateways forward to a network server; the server performs routing, deduplication and application delivery. With no gateway in range, an end device has no peer to hear it.

Meshtastic is a **peer mesh** with no gateway and no server. Every node both originates traffic and rebroadcasts what it receives, using **managed flooding**: a node repeats a packet it has not seen before, and a hop counter decrements on each relay so that packets terminate rather than circulate indefinitely. The default `hop_limit` is **3**, with a maximum of **7**. Per-hop distance depends on terrain, antenna placement and modem preset, so the area three hops cover is not a fixed figure.

The two invariants that keep flooding bounded are the **duplicate-suppression check** (a packet already seen is not repeated) and the **hop counter** (a packet whose counter reaches zero is not repeated). Raising `hop_limit` toward 7 multiplies the number of retransmissions each originated packet provokes, consuming shared airtime that every node on the frequency competes for.

The comparison is therefore not one of quality but of shape. A scatter of people and sensors that must reach *each other* off-grid is served by the mesh. Thousands of write-only sensors reporting through fixed infrastructure are served by LoRaWAN.

## Hardware

Meshtastic runs on ESP32, nRF52 and RP2040/RP2350 microcontrollers paired with a Semtech LoRa transceiver — the newer **SX126x** or the older **SX127x**. Common starting points:

- **Heltec LoRa 32 (V3)** — ESP32-S3 with SX1262 and an onboard OLED display.
- **LilyGO T-Beam** — ESP32 with a Semtech transceiver (SX127x or SX126x, depending on the variant), GPS and an 18650 cell holder.
- **RAK WisBlock (RAK4631)** — nRF52840 with SX1262; modular and low-power, suited to battery telemetry nodes.

ESP32 boards are the simplest to flash and the least expensive. nRF52 boards draw substantially less power, which is what makes a node running for months on a single cell feasible. The antenna must match the region's band: an 868 MHz antenna on a node configured for the United States 915 MHz band is mismatched and loses range.

## Flashing the firmware

The firmware version current at the time of writing is **v2.7.26.54e0d8d**, released **23 June 2026**. Releases appear roughly weekly, so the [releases page](https://github.com/meshtastic/firmware/releases) carries the current one.

The lowest-friction path is the **web flasher** at **<https://flasher.meshtastic.org>**, a WebSerial page supported in Chrome and Edge. The board is connected over USB, the exact device model and firmware version are selected from dropdowns, and the page drives the bootloader sequence.

For manual flashing of an ESP32 board, the firmware archive is downloaded and written with `esptool`:

```bash
pip install esptool
# some boards require holding BOOT and tapping RESET to enter download mode
esptool.py --port /dev/ttyUSB0 --baud 921600 write_flash 0x00 firmware-<board>-<ver>.bin
```

The archive also ships `device-install.sh`, which applies the correct flash offsets and writes the filesystem partition in addition to the application image; it is preferable to entering addresses by hand. nRF52 and RP2040 boards flash instead by copying a UF2 file onto the mass-storage bootloader volume.

## Region: the setting that gates transmission

Freshly flashed firmware **will not transmit until the region is set**. The device display shows a region-unset message and the radio stays silent. LoRa operates in license-free **industrial, scientific and medical (ISM) bands** whose allocations differ by country:

- **United States** — 902–928 MHz, up to 30 dBm
- **EU_868** — 869.4–869.65 MHz, 27 dBm, 10% duty cycle
- **EU_433** — 433 MHz
- **ANZ** — 915–928 MHz; further regions (JP, CN, IN and others) carry their own band and power limits

Setting the region and confirming the radio responds:

```bash
pip install --upgrade "meshtastic[cli]"
meshtastic --set lora.region US
meshtastic --set-owner "Saltmere-1"
meshtastic --info
```

`--info` prints the node's configuration, its channels and the mesh it can observe; `--nodes` prints the neighbour table. When the command-line interface cannot locate the board automatically, the port is given explicitly: `meshtastic --port /dev/ttyUSB0 --info`.

The default **modem preset** is `LONG_FAST`. The slower presets `LONG_SLOW` and `VERY_LONG_SLOW` trade throughput for range. **Every node intended to communicate must share the same region and the same modem preset**; radios differing in either cannot demodulate each other's transmissions.

## Channels and encryption

A Meshtastic channel bundles a name, a modem setting and a **pre-shared key (PSK)**. Traffic is encrypted with the Advanced Encryption Standard (AES): a **16-byte PSK selects AES-128, a 32-byte PSK selects AES-256**.

The stock **primary channel** — empty name, presented as "LongFast" — uses the publicly known default key `0x01` (base64 `AQ==`). Because that key is published in the source tree, **any party within radio range can decrypt traffic on the default channel**. It suits testing and public chat and nothing else. A distinct key is generated as follows:

```bash
# random 256-bit key on the primary channel
meshtastic --ch-set psk random --ch-index 0 --info

# or add a named secondary channel carrying its own key
meshtastic --ch-add sensors
meshtastic --ch-index 1 --ch-set psk random --info
```

A channel's exact configuration is transferred between nodes by exporting its URL and importing it verbatim:

```bash
meshtastic --info                     # prints the channel URL among the node's configuration
meshtastic --qr                       # the same URL as a scannable code
meshtastic --seturl https://meshtastic.org/e/#...   # apply it verbatim elsewhere
```

One coupling deserves emphasis: **a hash of the primary channel's name selects the LoRa frequency slot**. Renaming the primary channel therefore changes the frequency on which the node transmits, and a fleet whose primary channel names diverge splits across frequency slots even when region and preset agree.

```bash
meshtastic --sendtext "hello mesh"
meshtastic --sendtext "sensor node online" --ch-index 1
```

## Carrying sensor data over the mesh

The **Telemetry module** reads Inter-Integrated Circuit (I2C) sensors directly and broadcasts the readings as structured packets, without application code on the node. It supports more than thirty sensors, including the **PMSA003I** particulate-matter sensor, the BME280 and BME680, the SHT3x and SHT4x, and the INA2xx power monitors. The sensor is wired to the board's I2C pins and the module enabled:

```bash
meshtastic --set telemetry.environment_measurement_enabled true \
           --set telemetry.environment_screen_enabled true \
           --set telemetry.environment_update_interval 300
```

Every node on the channel, and anything bridged to MQTT, then receives the particulate and temperature readings, relayed hop by hop from wherever the sensor is sited.

A sensor the Telemetry module does not support is handled by running it on a second microcontroller and feeding the mesh through the **Serial module**, which passes bytes in and out over a universal asynchronous receiver-transmitter (UART) link. This separates concerns cleanly: one ESP32 runs the application firmware, the Meshtastic node acts purely as the radio.

The composite result is a sensor network with no cloud dependency and no gateway to maintain. Battery-powered nodes distributed across a neighbourhood each measure and relay, and readings propagate hop by hop toward whichever node is being read.

## Pitfalls

- **A freshly flashed node appears dead.** The region is unset, and firmware refuses to transmit until it is configured; the display carries a region-unset message rather than a radio fault.
- **Two nodes never see each other despite correct region.** The modem presets differ. `LONG_FAST` and `LONG_SLOW` use different spreading factors and bandwidths, so neither radio can demodulate the other.
- **Renaming the primary channel silently partitions the fleet.** The frequency slot is derived from a hash of the primary channel name, so a renamed node transmits on a different frequency than its unchanged peers.
- **Traffic on the stock channel is readable by strangers.** The default primary channel key `0x01` is published in the firmware source; anyone in range decrypts it.
- **Raising `hop_limit` to the maximum of 7 degrades the mesh.** Each additional hop multiplies retransmissions of every originated packet, and all nodes on the frequency contend for the same airtime.
- **An antenna cut for the wrong band costs range.** An 868 MHz antenna on a node configured for the United States 915 MHz band is mismatched, and the loss appears as reduced range rather than an error.
- **Hand-entered flash offsets omit the filesystem partition.** Writing only the application image at `0x00` skips what `device-install.sh` would also write.
