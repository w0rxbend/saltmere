---
title: "Reading a Modbus RTU Sensor over RS-485 from an ESP32"
date: 2026-08-14
track: iot-embedded
summary: "Most industrial and air-quality sensors talk Modbus RTU over half-duplex RS-485, not I2C. Here is the wiring — MAX485, the DE/RE direction pin, termination — and a runnable ESP32 sketch using eModbus to poll holding and input registers."
reading_time: 6
tags: [esp32, modbus-rtu, rs485, eModbus, industrial-iot]
sources:
  - title: "eModbus — Modbus library for RTU/ASCII/TCP (GitHub)"
    url: "https://github.com/eModbus/eModbus"
  - title: "eModbus — ModbusClientRTU API reference"
    url: "https://emodbus.github.io/modbusclient-rtu-api"
  - title: "MODBUS Application Protocol Specification — modbus.org"
    url: "https://www.modbus.org/modbus-specifications"
  - title: "RS485 with ESP32 & Arduino: Wiring and Modbus RTU — OpenELAB"
    url: "https://openelab.io/blogs/learn/rs485-with-esp32-arduino-modbus-rtu"
---

I2C sensors like the SEN5x are a joy, but step onto an industrial bench — CO2 transmitters, power meters, weather masts — and the wire that comes out is almost always **RS-485 carrying Modbus RTU**. It is a two-wire differential bus that runs hundreds of metres, and RTU is a compact binary framing that a $2 transceiver and one ESP32 UART can speak. Here is the whole path from bus to registers.

## Modbus RTU in one paragraph

Modbus is strictly master/client and slave/server: your ESP32 is the *client* that polls, and each sensor is a *server* with a 1-byte address (1–247). A request names a function code and a register range. The two you will use almost exclusively for reading are **0x03 Read Holding Registers** and **0x04 Read Input Registers** — both return 16-bit registers, the difference being convention (holding registers are read/write, input registers are read-only measurements). Every frame ends with a **CRC-16** (the RTU flavour, polynomial 0xA001); the library computes and checks it for you. Watch the addressing offset: a datasheet may call a value "40001" or "input register 30002" while the wire — and eModbus — want a **zero-based address**, so 40001 is address `0`. This off-by-one is the single most common integration bug.

## Half-duplex wiring with a MAX485

RS-485 is half-duplex on one differential pair (A/B), so exactly one device may drive the bus at a time. A MAX485 breakout maps cleanly to a UART plus one control GPIO:

```
ESP32                 MAX485                RS-485 bus
GPIO17 (TX2) --------> DI
GPIO16 (RX2) <-------- RO
GPIO4  ------------->  DE + /RE (tied)      A ----+---- sensor A
3V3    ------------->  VCC                  B ----+---- sensor B
GND    ------------->  GND                  GND --+---- sensor GND
```

`DE` enables the driver; `/RE` enables the receiver and is active-low. Tie them together and drive from one GPIO: **HIGH to transmit, LOW to receive.** eModbus flips this pin for you around each request. Two more physical rules matter on a real bus:

- **Termination:** a 120 Ω resistor across A/B at each of the two physical ends of the cable only — never in the middle.
- **Biasing:** one bias network for the whole bus defines the idle line state; do not enable per-module bias on every node.

Keep your USB serial console on `Serial` and give Modbus its own hardware UART (`Serial2` here). The GPIOs above are examples — an ESP32-S3 or C3 exposes different safe pins.

## The sketch: eModbus

[eModbus](https://github.com/eModbus/eModbus) is the actively maintained library built primarily for the ESP32. It is asynchronous: you fire a request with a token and results arrive in callbacks. Pass the DE/RE pin straight to the constructor.

```cpp
#include <Arduino.h>
#include "ModbusClientRTU.h"

#define DE_RE_PIN 4          // DE + /RE tied together
#define SENSOR_ID 1          // Modbus slave address
#define FIRST_REG 0          // zero-based: datasheet "40001" -> 0

// Constructor takes the RTS/direction pin directly
ModbusClientRTU MB(DE_RE_PIN);

void handleData(ModbusMessage response, uint32_t token) {
  // response layout: [addr][func][byteCount][data...]
  uint16_t temp_raw = 0, hum_raw = 0;
  response.get(3, temp_raw);   // first register, offset 3
  response.get(5, hum_raw);    // second register
  Serial.printf("temp=%.1f  hum=%.1f\n", temp_raw / 10.0, hum_raw / 10.0);
}

void handleError(Error err, uint32_t token) {
  ModbusError me(err);
  Serial.printf("Modbus error %02X: %s\n", (int)me, (const char*)me);
}

void setup() {
  Serial.begin(115200);
  Serial2.begin(9600, SERIAL_8N1, 16, 17);   // RX=16, TX=17

  MB.onDataHandler(&handleData);
  MB.onErrorHandler(&handleError);
  MB.setTimeout(2000);
  MB.begin(Serial2, 9600);                    // baud must match the bus
}

void loop() {
  // token, slaveID, functionCode, firstRegister, numRegisters
  Error err = MB.addRequest((uint32_t)millis(), SENSOR_ID,
                            READ_HOLD_REGISTER, FIRST_REG, 2);
  if (err != SUCCESS) {
    ModbusError me(err);
    Serial.printf("request rejected: %s\n", (const char*)me);
  }
  delay(2000);
}
```

Swap `READ_HOLD_REGISTER` for `READ_INPUT_REGISTER` to issue function code 0x04 instead of 0x03 — same call shape. `response.get(offset, var)` reads big-endian from the frame; for a 32-bit float spread across two registers, `get` a `float` at the right offset, and if the vendor uses word-swapped floats you may need to reorder the two 16-bit halves first.

## When nothing comes back

Ninety percent of first-run failures are three things: **baud/parity mismatch** (9600 8N1 is common but confirm the datasheet), **wrong slave address**, or **A/B swapped** — if you get consistent timeouts, try flipping A and B, it harms nothing. A cheap USB-RS485 dongle plus a desktop Modbus poller is worth keeping on the bench to prove the sensor answers before you blame your firmware.

**Try next:** add a second sensor at a different slave address on the same A/B pair, terminate only the two cable ends, and alternate `addRequest` calls between the two IDs — you now have a multi-drop bus, which is the entire point of RS-485.
