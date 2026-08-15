---
title: "Reading a Modbus RTU Sensor over RS-485 from an ESP32"
date: 2026-08-14
track: iot-embedded
summary: "Most industrial and air-quality sensors speak Modbus RTU over half-duplex RS-485 rather than I2C. The wiring — MAX485, the tied DE//RE direction pin, end-of-cable termination — and an ESP32 sketch using eModbus to poll holding and input registers."
reading_time: 7
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

**Gist.** Inter-integrated circuit (I2C) sensors assume a few centimetres of trace, so industrial instruments — carbon dioxide transmitters, power meters, weather masts — expose **Modbus remote terminal unit (RTU) framing over half-duplex RS-485** instead: one differential pair, many devices, cable runs measured in hundreds of metres. A microcontroller reaches that bus through a transceiver such as the MAX485, one hardware universal asynchronous receiver/transmitter (UART), and a single general-purpose input/output (GPIO) pin that switches the transceiver between driving and listening. The cost of the single pair is that the bus has no collision detection whatsoever: correctness depends on a strict poll-response discipline, on the direction pin being released at exactly the right instant, and on termination existing at the two physical cable ends and nowhere else.

## The protocol as a state machine

Modbus is strictly client/server — historically master/slave. The ESP32 is the **client** and issues every transaction; each sensor is a **server** identified by a one-byte address. Addresses **1 to 247** address a single server; **address 0 is the broadcast address**, which servers act on without replying. A server never speaks unless spoken to, which is what makes an undetectable-collision bus workable.

An RTU frame is minimal: **server address (1 byte), function code (1 byte), data, and a 16-bit cyclic redundancy check (CRC-16) using the reflected polynomial 0xA001**. There is no start delimiter and no length prefix. Frame boundaries are carried by *silence*: a frame is considered complete after a gap of **3.5 character times**, and a gap of more than **1.5 character times** inside a frame marks it as incomplete and causes it to be discarded. This is the mechanism behind an entire class of intermittent faults — anything that stalls the UART mid-frame for longer than 1.5 characters (a long interrupt, a blocking write to flash, a busy scheduler) destroys a frame that was otherwise electrically perfect.

Two function codes cover nearly all sensor reading: **0x03 Read Holding Registers** and **0x04 Read Input Registers**. Both return 16-bit registers, big-endian on the wire. The distinction is conventional rather than electrical: holding registers are readable and writable, input registers are read-only measurements. The specification caps a single read at **125 registers** for either code; a request for more is rejected rather than truncated.

Failure is explicit. When a server rejects a request it replies with the **function code with its high bit set (0x03 becomes 0x83) followed by a one-byte exception code** — illegal function, illegal data address, illegal data value, server device failure among them. **An exception response is a successful exchange at the transport layer**: the wiring, baud rate and address are all correct, and only the register range or the operation is wrong. Confusing an exception with a timeout sends diagnosis in the wrong direction.

The remaining trap is addressing. A datasheet may name a value "40001" or "input register 30002", using the traditional data-model numbering in which the leading digit encodes the register class and the numbering starts at one. **The wire — and eModbus — carry a zero-based address within the class**, so "40001" is address `0` and "30002" is address `1`. This off-by-one is a common first-run integration failure, and it does not always fail loudly: the server answers with an illegal-data-address exception, or returns the neighbouring register's value with no error at all.

## Half-duplex wiring with a MAX485

RS-485 is half-duplex over one differential pair, labelled A and B. **Exactly one device may drive the pair at any instant**; two simultaneous drivers contend and both frames are lost. A MAX485 breakout maps onto a UART plus one control GPIO:

```
ESP32                 MAX485                RS-485 bus
GPIO17 (TX2) --------> DI
GPIO16 (RX2) <-------- RO
GPIO4  ------------->  DE + /RE (tied)      A ----+---- sensor A
3V3    ------------->  VCC                  B ----+---- sensor B
GND    ------------->  GND                  GND --+---- sensor GND
```

`DE` enables the driver; `/RE` enables the receiver and is active-low. Tying them together and driving both from one GPIO makes the transceiver strictly one-directional: **HIGH transmits, LOW receives**. eModbus toggles this pin around each request.

The timing of the falling edge is load-bearing. The driver must stay enabled until the **last stop bit of the CRC has physically shifted out of the UART**. Releasing on the last `write()` call returning — which returns once bytes are buffered, not once they are on the wire — truncates the tail of the frame, and the server discards it on CRC failure. The symptom is a silent server with a scope trace showing a request that visibly stops short.

Two physical rules apply to a real bus:

- **Termination:** a 120 Ω resistor across A and B at each of the **two physical ends of the cable only**, never in the middle. Termination in the middle of a run loads the bus without suppressing reflections.
- **Biasing:** **one** bias network for the entire bus defines the idle line state. Enabling per-module bias resistors on every node pulls the differential voltage toward the bias rail and shrinks the receiver's usable margin.

The console serial port and the Modbus port must be different hardware UARTs — `Serial` for the universal serial bus (USB) console, `Serial2` for the bus below. The GPIO numbers above are examples; an ESP32-S3 or ESP32-C3 exposes a different set of safe pins.

## The sketch: eModbus

[eModbus](https://github.com/eModbus/eModbus) is an actively maintained Modbus library built primarily for the ESP32. It is asynchronous: a request is submitted with a caller-chosen token and the result arrives in a callback, so the polling loop never blocks on the bus. The direction pin is passed to the constructor.

```cpp
#include <Arduino.h>
#include "ModbusClientRTU.h"

#define DE_RE_PIN 4          // DE + /RE tied together
#define SENSOR_ID 1          // Modbus server address
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
  // token, serverID, functionCode, firstRegister, numRegisters
  Error err = MB.addRequest((uint32_t)millis(), SENSOR_ID,
                            READ_HOLD_REGISTER, FIRST_REG, 2);
  if (err != SUCCESS) {
    ModbusError me(err);
    Serial.printf("request rejected: %s\n", (const char*)me);
  }
  delay(2000);
}
```

The two callbacks separate the two failure classes described above: `handleError` receives both transport timeouts and protocol exceptions, and the error code distinguishes them. The token — `millis()` here — is opaque to the library and is the only means of correlating a response with the request that produced it once more than one request is in flight.

Substituting `READ_INPUT_REGISTER` for `READ_HOLD_REGISTER` issues function code 0x04 instead of 0x03 with an otherwise identical call. `response.get(offset, var)` reads big-endian from the raw frame, which is why the first register sits at offset 3: one address byte, one function byte, one byte-count byte precede the payload. A 32-bit float occupying two consecutive registers can be read by calling `get` with a `float` destination at that offset; vendors that transmit the two 16-bit halves in the opposite order require the halves to be swapped before conversion.

Extending to multi-drop is a matter of addresses, not wiring: a second sensor at a different server address on the same A/B pair, termination still only at the two cable ends, and `addRequest` calls alternating between the identifiers.

## Pitfalls

- **Baud rate, parity or stop-bit mismatch produces total silence, not corruption.** The receiver never assembles a byte at all, so the failure looks identical to a dead sensor or a broken wire.
- **A and B swapped yields consistent timeouts with no partial data.** Exchanging the two conductors does not damage the transceiver, so it is the cheapest hypothesis to eliminate.
- **A datasheet register number used verbatim gives an illegal-data-address exception, or silently returns the adjacent register.** "40001" is address 0, not 40001 and not 1.
- **Terminating every node instead of the two cable ends over-loads the driver.** Each 120 Ω resistor sits in parallel; enough of them pull the differential swing below the receiver threshold and reads become intermittent as the bus lengthens.
- **Releasing the DE//RE pin before the UART transmit shift register has emptied truncates the frame.** The server discards it on CRC failure and the client sees a timeout, which reads as a wiring fault.
- **Any stall longer than 1.5 character times inside a transmission invalidates the frame.** A blocking flash write or a long interrupt handler in the polling task produces failures that correlate with unrelated firmware activity rather than with the bus.
- **An exception response is not a communication failure.** Treating 0x83 plus an exception code as a timeout leads to re-checking wiring that is already correct, when the register range or the function code is what needs changing.
- **Sharing the console UART with the bus interleaves log output into Modbus frames.** Console writes are indistinguishable from frame bytes to the server, so the bus needs a UART of its own.
