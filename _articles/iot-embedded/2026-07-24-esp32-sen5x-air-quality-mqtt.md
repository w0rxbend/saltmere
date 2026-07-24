---
title: "An ESP32 + SEN5x air-quality node that speaks MQTT to your backend"
date: 2026-07-24
track: iot-embedded
summary: "One I2C sensor, one ESP32, and a JSON payload over MQTT gives you a fleet-ready air-quality node — PM2.5, VOC, NOx, temperature and humidity — for well under $40."
reading_time: 5
tags: [esp32, sensirion, sen5x, mqtt, air-quality]
sources:
  - title: "Sensirion embedded I2C driver for SEN5x"
    url: "https://github.com/Sensirion/arduino-i2c-sen5x"
  - title: "M5Stack Air Quality Kit v1.1 (SEN55 + SCD40)"
    url: "https://www.cnx-software.com/2025/09/14/m5stack-air-quality-kit-v1-1-features-sensirion-sen55-environmental-sensor-and-scd40-co2-sensor/"
  - title: "Eclipse Mosquitto MQTT broker"
    url: "https://mosquitto.org/"
---

The SEN5x family (SEN54/SEN55) is the sweet spot for indoor air quality: a single I2C module gives you PM1/PM2.5/PM4/PM10, VOC and NOx indices, plus temperature and humidity, already calibrated. Pair it with an ESP32 and the only real design decision left is *how the reading gets off the device* — and for a fleet, the answer is MQTT.

## Wiring

I2C, four wires: SEN5x `SDA→GPIO21`, `SCL→GPIO22`, `VCC→5V`, `GND→GND`. The SEN55 wants 5V for its fan; the I2C lines are 3.3V-logic friendly. That's it.

## The firmware, in outline

Use Sensirion's official driver plus PubSubClient. The loop is: read the sensor, build a compact JSON document, publish it to a per-device topic, sleep. Keep the topic hierarchical so the backend can subscribe with wildcards.

```cpp
#include <WiFi.h>
#include <PubSubClient.h>
#include <SensirionI2CSen5x.h>
#include <Wire.h>

SensirionI2CSen5x sen5x;
WiFiClient net; PubSubClient mqtt(net);
const char* DEVICE = "node-kitchen-01";

void publishReading() {
  float pm1, pm25, pm4, pm10, hum, temp, voc, nox;
  if (sen5x.readMeasuredValues(pm1, pm25, pm4, pm10, hum, temp, voc, nox)) return;

  char buf[256];
  snprintf(buf, sizeof(buf),
    "{\"pm25\":%.1f,\"pm10\":%.1f,\"voc\":%.0f,\"nox\":%.0f,\"t\":%.1f,\"rh\":%.0f}",
    pm25, pm10, voc, nox, temp, hum);

  // topic: aq/<device>/telemetry  — retained=false, QoS 0 is fine for telemetry
  char topic[64]; snprintf(topic, sizeof(topic), "aq/%s/telemetry", DEVICE);
  mqtt.publish(topic, buf);
}
```

`setup()` does the usual: `Wire.begin()`, `sen5x.begin(Wire)`, `sen5x.startMeasurement()`, connect WiFi, `mqtt.setServer(broker, 1883)`. Then `publishReading()` every 10–60 s.

## Why MQTT is the right seam for a fleet

This is where the "massive IoT backend" concern starts, and getting three things right on the device saves you enormous pain later:

- **Topic design is your schema.** `aq/<device>/telemetry` lets the backend subscribe to `aq/+/telemetry` for everything, or `aq/node-kitchen-01/#` for one node. Bake location/role into the device id, not the payload.
- **Last Will & Testament = free liveness.** Register an LWT on connect (`aq/<device>/status` → `offline`) and publish `online` yourself. Now the broker announces a dead node the moment its TCP session drops — no polling.
- **QoS is a cost knob.** Telemetry you sample every 30 s can be QoS 0 (fire and forget); a command that toggles the fan should be QoS 1. Don't pay for delivery guarantees you don't need at fleet scale.

## From one node to a fleet

Point the nodes at a [Mosquitto](https://mosquitto.org/) broker, then bridge that into whatever stores the series. A common, cheap pipeline is MQTT → Telegraf → a time-series store (InfluxDB/TimescaleDB) → Grafana; the same telemetry topic feeds it unchanged. The device firmware never needs to know any of that exists — which is exactly the decoupling MQTT is for.

**Try next:** add the LWT and a `aq/<device>/status` heartbeat, then kill power to one node and watch the broker flip it to `offline`. That single behavior is the foundation of fleet health monitoring — the observability track picks it up from there.
