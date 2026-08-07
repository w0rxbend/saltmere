---
title: "Meshtastic: an off-grid encrypted LoRa mesh on a $20 ESP32"
date: 2026-08-07
track: iot-embedded
summary: "No internet, no cell, no gateway, no network server — just cheap ESP32 boards talking to each other over LoRa and relaying each other's traffic for miles. Flash the firmware, set your region, pick a channel, and you have an encrypted text-and-telemetry mesh that can carry your air-quality readings where Wi-Fi and LoRaWAN can't reach."
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

Last week's node put an ESP32-S3 and an SX1262 on **LoRaWAN** — kilometers of range, but you needed The Things Network gateways overhead and a network server behind them. Take away the infrastructure and you get **Meshtastic**: the same cheap LoRa hardware, but the nodes *are* the network. Each board relays its neighbors' packets, so a message hops device-to-device across a town with no gateway, no SIM, no internet. It's encrypted text and position and telemetry, running on boards you can buy for the price of a pizza.

This is not LoRaWAN with the serial numbers filed off. It's a different animal, and the difference is the whole point.

## Mesh, not LoRaWAN

LoRaWAN is a **star topology**: end devices talk only to gateways, gateways forward to a network server, and the server does the routing, dedup, and app delivery. Pull the gateway and your node is shouting into a void.

Meshtastic is a **peer mesh**. There is no gateway and no server. Every node both originates traffic and rebroadcasts what it hears, using *managed flooding* — a node repeats a packet it hasn't seen before, with a hop counter that decrements each relay so packets die instead of circulating forever. Default `hop_limit` is 3 (max 7). Three hops of a few hundred meters to a few kilometers each is a surprising amount of coverage from a handful of $20 radios on rooftops.

The trade: Meshtastic's mesh is chattier and lower-capacity than a LoRaWAN star, and there's no cloud backend unless you bolt on MQTT yourself. For a scatter of people and sensors that need to reach *each other* off-grid, the mesh wins. For thousands of write-only sensors phoning home through fixed infrastructure, LoRaWAN wins.

## Hardware

Meshtastic runs on ESP32, nRF52, and RP2040/RP2350 boards paired with a Semtech LoRa transceiver — the newer **SX126x** or older **SX127x**. Popular starting points:

- **Heltec LoRa 32 (V3)** — ESP32-S3 + SX1262, onboard OLED. Cheapest way in.
- **LilyGO T-Beam** — ESP32 + SX1262 + GPS + 18650 holder. The classic field node.
- **RAK WisBlock (RAK4631)** — nRF52840 + SX1262. Low-power, modular, great for battery telemetry nodes.

ESP32 boards are the easiest to flash and the cheapest; nRF52 boards sip far less power if you want a node that lives for months on a cell. Match the radio to your region's antenna (an 868 MHz antenna on a US 915 node will cost you range).

## Flash the firmware

Current firmware as of this writing is **v2.7.26.54e0d8d** (released 23 June 2026); the project ships roughly weekly, so check the [releases page](https://github.com/meshtastic/firmware/releases) for the latest. All 2.7.x releases are tagged alpha in the GitHub sense but are what everyone runs.

The path of least resistance is the **web flasher** at **<https://flasher.meshtastic.org>**. It's a WebSerial page (Chrome/Edge): plug the board in over USB, pick your exact device from the dropdown, choose a firmware version, and click flash. It handles the bootloader dance for you.

If you'd rather drive it yourself on ESP32, download the firmware zip for your board and use `esptool`:

```bash
pip install esptool
# hold BOOT, tap RESET to enter download mode first on some boards
esptool.py --port /dev/ttyUSB0 --baud 921600 write_flash 0x00 firmware-<board>-<ver>.bin
```

The zip also ships a `device-install.sh` that wraps the correct offsets and flashes the SPIFFS/littlefs partition too — prefer it over hand-typing addresses. nRF52 and RP2040 boards flash by drag-and-dropping a UF2 file onto the mass-storage bootloader instead.

## The one setting you cannot skip: region

Fresh firmware **will not transmit** until you set the region. The node screen literally shows a "region unset" message and stays silent — this is deliberate, because LoRa uses license-free **ISM bands** that differ by country and you must stay legal:

- **US** — 902–928 MHz, up to 30 dBm
- **EU_868** — 869.4–869.65 MHz, 27 dBm, 10% duty cycle
- **EU_433** — 433 MHz
- **ANZ** — 915–928 MHz; **JP** — 920.5–923.5 MHz; and so on

Set it, then confirm the radio is alive:

```bash
pip install --upgrade "meshtastic[cli]"
meshtastic --set lora.region US
meshtastic --set-owner "Saltmere-1"
meshtastic --info
```

`--info` dumps the node's config, its channels, and the mesh it can see; `--nodes` prints the neighbor table. If the CLI can't find your board, pass it explicitly: `meshtastic --port /dev/ttyUSB0 --info`.

The default **modem preset** is `LONG_FAST`, a sane speed/range balance. Slower presets (`LONG_SLOW`, `VERY_LONG_SLOW`) buy range at the cost of throughput. *Every node that wants to talk to every other node must share the same region and preset* — mismatched radios are simply deaf to each other.

## Channels and encryption

A Meshtastic channel bundles a name, a modem setting, and a **pre-shared key (PSK)**. Traffic is AES-encrypted — a 16-byte PSK gives AES-128, a 32-byte PSK gives AES-256. The catch: the stock **primary channel** (empty name, "LongFast") uses the publicly known default key `0x01` (base64 `AQ==`). It's fine for testing and public chat, but *anyone can decrypt it* because the key is in the source. For anything private, generate a real key:

```bash
# strong random 256-bit key on the primary channel
meshtastic --ch-set psk random --ch-index 0 --info

# or add a named secondary channel with its own key
meshtastic --ch-add sensors
meshtastic --ch-set name telemetry --ch-index 1
meshtastic --ch-index 1 --ch-set psk 0x1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b1a1a1a1a2b2b2b2b --info
```

Share a channel's exact config with another node by exporting its URL and importing it on the other device:

```bash
meshtastic --info                     # prints the channel URL (or QR)
meshtastic --seturl https://meshtastic.org/e/#...   # apply it verbatim elsewhere
```

Note a subtlety worth remembering: a hash of the *primary* channel's name selects the LoRa frequency slot, so renaming the primary channel actually shifts the frequency you transmit on. Keep it consistent across your fleet. Then talk:

```bash
meshtastic --sendtext "hello mesh"
meshtastic --sendtext "sensor node online" --ch-index 1
```

## Shipping sensor data over the mesh

Here's where the air-quality thread rejoins. Meshtastic's **Telemetry module** reads I2C sensors directly and broadcasts the values as structured packets — no custom code. It natively supports 30-plus sensors including the **PMSA003I** particulate-matter sensor, plus BME280/BME680, SHT3x/SHT4x, INA2xx power monitors, and more. Wire the sensor to the board's I2C pins and enable it:

```bash
meshtastic --set telemetry.environment_measurement_enabled true \
           --set telemetry.environment_screen_enabled true \
           --set telemetry.environment_update_interval 300
```

Now every node on the channel — and anything bridged to MQTT — sees your PM2.5 and temperature readings appear as telemetry, hop-relayed from wherever the sensor lives. For a sensor Meshtastic doesn't know, run it on a second MCU and feed the mesh through the **Serial module**, which pipes bytes in and out over UART. That's the clean way to marry an ESP32 running your own air-quality firmware to a Meshtastic node acting purely as the radio.

The result is an air-quality network with no cloud dependency and no gateway to maintain: drop battery-powered PM sensor nodes across a neighborhood, and each one both measures and relays, the readings walking home hop by hop.

**Try next:** flash two Heltec V3 boards at flasher.meshtastic.org, set both to your region on the same random-PSK channel, and run `meshtastic --sendtext` from one while watching `--info` on the other — then hang a PMSA003I off one, enable environment telemetry, and confirm the PM2.5 numbers show up on the second node across the room.
