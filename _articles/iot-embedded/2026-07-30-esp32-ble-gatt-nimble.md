---
title: "A BLE GATT sensor server on the ESP32 with NimBLE"
date: 2026-07-30
track: iot-embedded
summary: "An ESP32 advertising an Environmental Sensing service and pushing air-quality readings to a phone over BLE notifications, with no broker, Wi-Fi or cloud: the GATT object model, the NimBLE and Bluedroid trade-off on a memory-tight board, and a NimBLE-Arduino 2.x sketch."
reading_time: 6
tags: [esp32, ble, nimble, gatt, bluetooth, air-quality]
sources:
  - title: "NimBLE-Arduino (h2zero)"
    url: "https://github.com/h2zero/NimBLE-Arduino"
  - title: "NimBLE-Arduino 1.x to 2.x Migration Guide"
    url: "https://github.com/h2zero/NimBLE-Arduino/blob/master/docs/1.x_to2.x_migration_guide.md"
  - title: "Bluetooth LE Overview (NimBLE vs Bluedroid) — ESP-IDF Programming Guide (stable)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/ble/overview.html"
  - title: "ESP32 BLE Peripheral: Environmental Sensing Service — Random Nerd Tutorials"
    url: "https://randomnerdtutorials.com/esp32-ble-server-environmental-sensing-service/"
  - title: "Bluetooth SIG Assigned Numbers"
    url: "https://www.bluetooth.com/specifications/assigned-numbers/"
---

**Gist.** An air-quality node that is read by a phone standing next to it needs no Wi-Fi credentials, no broker and no backend, but Bluetooth Low Energy (BLE) does not offer a byte stream to write into. The peripheral instead publishes a small typed database — the Generic Attribute Profile (GATT) — of services, characteristics and descriptors, and the central subscribes to the values it wants. The cost is that every value must be given a stable identifier and a fixed binary encoding before it can be transmitted at all, and reusing a Bluetooth Special Interest Group (SIG) identifier means adopting the SIG's encoding rather than a convenient one.

## The GATT object model

A BLE device that holds data is a **peripheral** (here, the ESP32); the device that connects to it is a **central** (the phone). The peripheral exposes a **GATT server**, a tree of **services**, each containing **characteristics**, each optionally carrying **descriptors**. A characteristic is one addressable value identified by a universally unique identifier (UUID) and carrying a set of properties — `READ`, `WRITE`, `NOTIFY` and others.

The distinction that shapes the firmware is between reading and subscribing. A `READ` characteristic is pulled by the central, one request per value. A `NOTIFY` characteristic is **pushed by the peripheral at times of its own choosing**, and the central opts in exactly once. The mechanism for that opt-in is a specific descriptor, the **Client Characteristic Configuration Descriptor (CCCD), UUID `0x2902`**: the central writes a bit into it, and the peripheral's `notify()` call transmits only while that bit is set for the connection. The invariant is one-directional: **the peripheral never learns that a value was consumed, only that a subscription exists.**

## NimBLE and Bluedroid

The ESP32 ships with two host stacks. Bluedroid is the default and also carries Bluetooth Classic; NimBLE is Apache Mynewt's BLE-only host, ported by Espressif and wrapped for Arduino by h2zero. Espressif's own comparison states: *"Although both support Bluetooth LE, ESP-NimBLE requires less heap and flash size."* On a board where the remaining random-access memory (RAM) also has to hold a sensor driver and application state, that difference is what decides the choice; **NimBLE is BLE-only, so Bluetooth Classic profiles such as Serial Port Profile (SPP) or Advanced Audio Distribution Profile (A2DP) still require Bluedroid.** The `NimBLE-Arduino` API stays close to the older `BLEDevice` one, so porting is largely mechanical.

The sketch below targets the **2.x line of NimBLE-Arduino**. The major version is load-bearing: 2.x changed several signatures relative to the 1.x tutorials still in circulation.

## Signature changes in NimBLE-Arduino 2.x

A 1.x sketch does not compile against 2.x. The changes that affect a GATT server:

- **Connection callbacks carry `NimBLEConnInfo&`.** `onConnect(NimBLEServer*)` became `onConnect(NimBLEServer*, NimBLEConnInfo&)`, and `onDisconnect` gains an `int reason`.
- **`notify()` dropped its `bool` argument.** In 1.x, `notify(false)` sent an indication. In 2.x notifications and indications are separate calls, `notify()` and `indicate()`.
- **Advertising `start()` no longer takes a callback pointer**; completion is registered through `setAdvertisingCompleteCallback()`. `setMinPreferred` and `setMaxPreferred` collapsed into `setPreferredParams()`.
- **Property flags live in the `NIMBLE_PROPERTY::` namespace** (`NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY`) rather than the older `BLECharacteristic::PROPERTY_*` constants.

One behaviour carries over from 1.x: **creating a characteristic with the `NOTIFY` property causes NimBLE to add the `0x2902` CCCD automatically.** The descriptor is not hand-constructed the way older Bluedroid examples construct it.

## The sketch

The firmware advertises an **Environmental Sensing service (`0x181A`)** containing a **Temperature characteristic (`0x2A6E`)** and notifies a fresh reading every two seconds. `0x2A6E` is a SIG-assigned characteristic whose value is **a `sint16` in units of 0.01 °C**, so 23.5 °C is transmitted as the integer `2350`. A SEN5x temperature output maps onto that directly.

```cpp
#include <NimBLEDevice.h>

static NimBLECharacteristic* tempChar;

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& info) override {
    Serial.printf("central connected: %s\n", info.getAddress().toString().c_str());
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& info, int reason) override {
    NimBLEDevice::startAdvertising();   // advertising stops on connect; restart it
  }
};

void setup() {
  Serial.begin(115200);
  NimBLEDevice::init("aq-node-01");

  NimBLEServer* server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCB());

  // Environmental Sensing service -> Temperature characteristic
  NimBLEService* ess = server->createService((uint16_t)0x181A);
  tempChar = ess->createCharacteristic(
      (uint16_t)0x2A6E,
      NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);   // CCCD added automatically
  ess->start();

  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(ess->getUUID());   // lets a scanner filter on 0x181A
  adv->setName("aq-node-01");
  adv->start();
}

void loop() {
  float tempC = readSen5xTemperature();            // sensor read
  int16_t t   = (int16_t)lround(tempC * 100.0);    // 0x2A6E: sint16, 0.01 C
  tempChar->setValue((uint8_t*)&t, sizeof(t));
  tempChar->notify();   // transmits only if a client subscribed via the CCCD
  delay(2000);
}
```

After flashing, a generic scanner such as nRF Connect or LightBlue shows `aq-node-01` advertising service `0x181A`. Connecting, selecting the Temperature characteristic and enabling notifications produces a value that updates every two seconds. **An unconditional `notify()` in `loop()` is safe when no client is subscribed**: with the CCCD bit clear the call transmits nothing, so guarding it with a separate subscription flag changes no observable behaviour.

## Encoding

The most frequent defect in this shape of firmware is passing a `float` directly to `setValue()` and observing an implausible reading on the phone. SIG-assigned characteristics have defined binary formats, and generic BLE applications decode against those formats rather than against the peripheral's intent. `0x2A6E` is a little-endian `sint16` at 0.01 °C resolution, which is what the `* 100` scaling and the `int16_t` cast produce. **A custom 128-bit UUID carries no such contract**: both ends are written by the same author and any encoding is admissible. Reusing a SIG UUID transfers that freedom to the specification.

Extending the node follows the same three steps: a second characteristic under the same `0x181A` service for particulate matter — either the SIG's particulate-matter concentration UUID with its defined format, or a custom 128-bit UUID carrying a plain `uint16_t` in µg/m³ — notified from `loop()` alongside the temperature, so a single connection streams the whole SEN5x reading.

## Pitfalls

- **A `float` written into a SIG characteristic renders as an absurd temperature.** The client decodes `0x2A6E` as a little-endian `sint16` scaled by 0.01, so the four bytes of an IEEE-754 `float` are read as two unrelated integers.
- **Notifications silently stop after the first disconnect if advertising is not restarted.** The peripheral ceases advertising once a central connects, so without the `startAdvertising()` call in `onDisconnect`, the device becomes invisible to the next scan.
- **A 1.x callback signature compiles as a new method rather than an override.** `onConnect(NimBLEServer*)` does not match the 2.x virtual method, so the callback is never invoked; declaring the method `override` turns the mismatch into a compile error instead of silence.
- **`notify(false)` from a 1.x example does not send an indication under 2.x.** The `bool` parameter no longer exists; indications require the separate `indicate()` call.
- **Hand-creating a `0x2902` descriptor on a `NOTIFY` characteristic duplicates one NimBLE already added.** The CCCD is created automatically from the `NOTIFY` property.
- **A characteristic created without the `NOTIFY` property has no CCCD**, so subscription from the client fails and `notify()` never transmits, with no error surfaced on the peripheral.
