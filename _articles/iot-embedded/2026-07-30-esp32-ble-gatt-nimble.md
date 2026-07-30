---
title: "A BLE GATT sensor server on the ESP32 with NimBLE"
date: 2026-07-30
track: iot-embedded
summary: "No broker, no Wi-Fi, no cloud — just an ESP32 advertising an Environmental Sensing service and pushing air-quality readings straight to a phone over BLE notifications. Here's the GATT model, why NimBLE beats Bluedroid on a memory-tight board, and a complete NimBLE-Arduino 2.x sketch."
reading_time: 5
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

Not every air-quality node wants to be on the network. Sometimes you just want to walk up to a sensor with a phone, see the current PM2.5, and walk away — no Wi-Fi credentials, no broker, no backend. That's the job BLE was built for, and the ESP32 speaks it natively. The trick is understanding that BLE isn't a socket you write bytes into: it's a small typed database that the peripheral *publishes* and the phone *reads and subscribes to*. That database is GATT.

## The GATT model in one paragraph

A BLE device that holds data is a **peripheral** (the ESP32); the phone that connects to it is the **central**. The peripheral exposes a **GATT server**: a tree of **Services**, each containing **Characteristics**, each optionally carrying **Descriptors**. A characteristic is one addressable value with a UUID and a set of properties — `READ`, `WRITE`, `NOTIFY`, and so on. The central doesn't poll a `NOTIFY` characteristic; it *subscribes* once, and from then on the peripheral pushes new values whenever it wants. The mechanism is a special descriptor, the Client Characteristic Configuration Descriptor (CCCD, UUID `0x2902`): the phone writes a bit into it to say "start notifying me," and the ESP32's `notify()` calls only actually transmit when that bit is set. Get that mental model right and the code writes itself.

## Why NimBLE, not Bluedroid

The ESP32 ships with two host stacks. Bluedroid is the classic default and also carries Bluetooth Classic; NimBLE is Apache Mynewt's BLE-only host, ported by Espressif and wrapped for Arduino by h2zero. On a sensor node you almost never want Bluedroid. Espressif's own guidance is blunt: *"Although both support Bluetooth LE, ESP-NimBLE requires less heap and flash size."* In practice that's the difference between tens of kilobytes of RAM you keep for your sensor driver, TLS, and buffers, versus tens of kilobytes the BLE stack eats before your code even runs. NimBLE was designed for constrained devices from the start. Unless you specifically need Bluetooth Classic (SPP, A2DP), NimBLE is the correct choice on an ESP32 — and the `NimBLE-Arduino` library keeps the API close enough to the old `BLEDevice` one that porting is mechanical.

I targeted **NimBLE-Arduino 2.5.1** for this. That matters, because the 2.x line changed several signatures from the 1.x tutorials you'll find scattered around.

## What changed in NimBLE-Arduino 2.x

If you copy a 1.x sketch it won't compile against 2.x. The changes that bite a GATT server:

- **Connection callbacks now carry `NimBLEConnInfo&`.** `onConnect(NimBLEServer*)` became `onConnect(NimBLEServer*, NimBLEConnInfo&)`; `onDisconnect` gains an `int reason`.
- **`notify()` dropped its `bool` argument.** In 1.x you passed `notify(false)` to send an indication. In 2.x, notifications and indications are separate calls — `notify()` and `indicate()`.
- **Advertising `start()` no longer takes a callback pointer**; register completion via `setAdvertisingCompleteCallback()`. And `setMinPreferred`/`setMaxPreferred` collapsed into a single `setPreferredParams()`.
- **Property flags live in the `NIMBLE_PROPERTY::` namespace** (`NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY`), not the old `BLECharacteristic::PROPERTY_*` constants.

One convenience carries over: create a characteristic with the `NOTIFY` property and NimBLE adds the `0x2902` CCCD for you automatically. You do not hand-create the descriptor the way older Bluedroid examples do.

## The sketch

This advertises an **Environmental Sensing** service (`0x181A`) with a **Temperature** characteristic (`0x2A6E`) and notifies a fresh reading every two seconds. `0x2A6E` is a Bluetooth SIG standard characteristic: a `sint16` in units of 0.01 °C, so 23.5 °C is transmitted as the integer `2350`. That's the value your SEN5x's temperature output maps onto directly — and once this works, a second characteristic for PM2.5 is the same three lines.

```cpp
#include <NimBLEDevice.h>

static NimBLECharacteristic* tempChar;

class ServerCB : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer* s, NimBLEConnInfo& info) override {
    Serial.printf("central connected: %s\n", info.getAddress().toString().c_str());
  }
  void onDisconnect(NimBLEServer* s, NimBLEConnInfo& info, int reason) override {
    NimBLEDevice::startAdvertising();   // let the phone find us again
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
  adv->addServiceUUID(ess->getUUID());   // so a scanner can filter for us
  adv->setName("aq-node-01");
  adv->start();
}

void loop() {
  float tempC = readSen5xTemperature();            // your sensor read
  int16_t t   = (int16_t)lround(tempC * 100.0);    // 0x2A6E: sint16, 0.01 C
  tempChar->setValue((uint8_t*)&t, sizeof(t));
  tempChar->notify();   // transmits only if a client subscribed via the CCCD
  delay(2000);
}
```

Flash it, open nRF Connect or LightBlue on your phone, scan, and you'll see `aq-node-01` advertising service `0x181A`. Connect, tap the Temperature characteristic, enable notifications, and the value updates live every two seconds. Note that `notify()` is cheap when nobody's listening — with no subscribed client the CCCD bit is clear and the call is effectively a no-op, so you can leave it in `loop()` unconditionally without wasting radio time.

## Encoding is the part people get wrong

The single most common bug here is shipping a `float` straight into `setValue()` and then wondering why the phone shows garbage. Standard SIG characteristics have *defined* binary formats, and the generic BLE apps decode against them. `0x2A6E` is little-endian `sint16` at 0.01 °C resolution — hence the `* 100` and the `int16_t` cast above. If you invent your own characteristic (a custom 128-bit UUID for a raw PM2.5 µg/m³ value, say), you own the encoding on both ends and can send whatever you like — but the moment you reuse a SIG UUID, honor its format or generic clients will misread it.

**Try next:** add a second characteristic under the same `0x181A` service for PM2.5 — either the SIG's Particulate Matter concentration UUID with its defined format, or a custom 128-bit UUID carrying a plain `uint16_t` µg/m³ — and notify both from `loop()`, so one subscription streams your whole SEN5x reading to the phone at once.
