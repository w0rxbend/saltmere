---
title: "An ESP32 and SEN5x air-quality node reporting over MQTT"
date: 2026-07-24
track: iot-embedded
summary: "One I2C sensor, one ESP32 and a JSON payload over MQTT produce a fleet-ready air-quality node reporting PM2.5, VOC and NOx indices, temperature and humidity."
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

**Gist.** Indoor air quality requires several distinct measurements — particulate mass, volatile organic compounds, nitrogen oxides, temperature and relative humidity — and combining discrete sensors for each multiplies calibration work. The Sensirion SEN5x family (SEN54, SEN55) collapses them into **one factory-calibrated module on a single inter-integrated circuit (I2C) bus**, leaving the transport as the only substantial design decision; publishing each reading to a per-device Message Queuing Telemetry Transport (MQTT) topic decouples firmware from storage entirely. The cost is that the device gains no acknowledgement that anything consumed its data: at the quality-of-service level appropriate for telemetry, a publish that vanishes is indistinguishable at the node from one that arrived.

## The sensor interface

The SEN5x exposes PM1, PM2.5, PM4 and PM10 particulate channels over I2C; the SEN54 adds temperature, humidity and a volatile-organic-compound (VOC) index, and the SEN55 adds a nitrogen-oxide (NOx) index on top of those. The driver's read call has the same eight output parameters for every variant, so a payload built from it can carry a channel the attached module does not measure. Wiring is four conductors: `SDA→GPIO21`, `SCL→GPIO22`, `VCC→5V`, `GND→GND`. The **5 V rail is required by the module's fan**, which draws the sample stream past the optical particulate cell; the I2C lines themselves are compatible with the ESP32's 3.3 V logic, so no level shifter is involved.

Two consequences follow from the fan. First, the particulate channels are meaningful only while the fan runs, which is why the driver separates `begin()` from `startMeasurement()` — measurement is a mode the device enters, not a property it always has. Second, the fan and the heated elements sit inside the same enclosure as the temperature and humidity sensor, so **the reported temperature is the temperature inside the module**, not of the room, unless the module's own compensation is configured for the installation.

The VOC and NOx outputs are **indices, not concentrations**. They are derived from a gas-sensor response referenced against the sensor's own recent history, so a value carries meaning relative to that device's baseline rather than as an absolute chemical measurement. Two nodes in the same room can therefore disagree on index while agreeing on particulates, and neither is faulty.

## Firmware shape

The control loop is: read the sensor, serialise a compact JSON document, publish it to a per-device topic, wait. Sensirion's official Arduino driver handles the I2C framing; PubSubClient handles the MQTT session.

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
  // driver convention: 0 means success, non-zero is an error code
  if (sen5x.readMeasuredValues(pm1, pm25, pm4, pm10, hum, temp, voc, nox)) return;

  char buf[256];
  snprintf(buf, sizeof(buf),
    "{\"pm25\":%.1f,\"pm10\":%.1f,\"voc\":%.0f,\"nox\":%.0f,\"t\":%.1f,\"rh\":%.0f}",
    pm25, pm10, voc, nox, temp, hum);

  // topic: aq/<device>/telemetry  — retained=false, QoS 0 for telemetry
  char topic[64]; snprintf(topic, sizeof(topic), "aq/%s/telemetry", DEVICE);
  mqtt.publish(topic, buf);
}
```

`setup()` performs `Wire.begin()`, `sen5x.begin(Wire)`, `sen5x.startMeasurement()`, the Wi-Fi association, and `mqtt.setServer(broker, 1883)`. `publishReading()` then runs every 10–60 s.

Two details in that snippet are load-bearing. The **early `return` on a non-zero driver result discards the whole sample rather than publishing partially populated floats** — a failed read leaves the output parameters unspecified, and forwarding them would inject values into the time series that no sensor produced. And `snprintf` with an explicit buffer size truncates rather than overruns; a truncated JSON document is malformed and will be rejected downstream, which is a visible failure rather than a silent memory corruption.

The MQTT client also requires `mqtt.loop()` to be called regularly. PubSubClient does no work on its own thread: the keepalive PINGREQ, the reading of incoming packets and the connection bookkeeping all happen inside `loop()`. A control loop that publishes and then blocks in a long `delay()` will be disconnected by the broker for missing keepalives even though it is publishing successfully.

## Why MQTT is the seam for a fleet

Three device-side decisions determine how much the backend can do without firmware changes.

**Topic design is the schema.** `aq/<device>/telemetry` allows a subscriber to take `aq/+/telemetry` for every node's telemetry, or `aq/node-kitchen-01/#` for one node's entire subtree. The single-level wildcard `+` matches exactly one segment and the multi-level `#` matches the remainder, so **the segmentation chosen at publish time fixes which groupings are addressable later**. Encoding location and role in the device identifier rather than the payload keeps those groupings available to the broker, which routes on topic and never parses the body.

**Last Will and Testament (LWT) supplies liveness without polling.** The client registers a will message at connect time — for instance `aq/<device>/status` carrying `offline` — and the broker publishes it on the client's behalf when the session terminates other than by a clean disconnect. The node publishes `online` to the same topic itself once connected. The resulting invariant is that **the last message published to the status topic reflects the broker's view of the session, updated by whichever party ended it** — with the retain flag set on both the will and the node's own `online` message, a subscriber that connects later sees that state immediately rather than waiting for the next transition. Detection is bounded by the keepalive interval: MQTT specifies that the server may treat the client as disconnected once one and a half times the keepalive period elapses without a packet from it, so a node whose power is cut is reported offline no sooner than that.

**Quality of service is a cost knob.** QoS 0 delivers at most once with no acknowledgement; QoS 1 delivers at least once and requires a PUBACK, so the publisher must retain the message and retransmit until acknowledged. For a channel sampled every 30 s, a lost sample is one gap in a series that has many more points; for a command that toggles the fan, non-delivery is a state divergence between the operator's intent and the device. **The asymmetry is in the consequence of loss, not in the value of the message.**

## From one node to a fleet

Nodes point at a [Mosquitto](https://mosquitto.org/) broker, which is bridged into whatever stores the series — a common pipeline is MQTT to Telegraf to a time-series store such as InfluxDB or TimescaleDB, then Grafana. The same telemetry topic feeds that chain unchanged, and the firmware holds no knowledge of any element beyond the broker address. Replacing the storage layer requires no device flash.

## Pitfalls

- **Publishing a failed read.** If the driver's non-zero return is ignored, the output floats are unspecified and the payload carries whatever was on the stack, appearing downstream as a plausible reading rather than an outage.
- **Blocking the MQTT client.** A `delay()` spanning the sample interval starves `mqtt.loop()`, the keepalive lapses, and the broker closes the session — which fires the LWT and marks a healthy node offline.
- **Powering the SEN55 from 3.3 V.** The module's supply is specified as a 5 V rail, below which the fan that draws the sample stream past the optical cell is out of specification; the resulting particulate readings are not the ones the module is characterised for, and nothing in the I2C protocol reports the condition.
- **Treating VOC or NOx index as a concentration.** The index is referenced to the individual sensor's recent history, so alert thresholds copied between devices, or applied immediately after power-up before a baseline exists, fire on the sensor's adaptation rather than on air quality.
- **Reading the module's temperature as room temperature.** The sensing element shares an enclosure with the fan and heated elements, so an uncompensated installation reports a biased value that is stable and therefore easy to mistake for correct.
- **Flat topic names.** Publishing to `telemetry/node-kitchen-01` instead of `aq/<device>/telemetry` makes per-device subtrees unaddressable: `#` after a device segment can no longer scope status, commands and telemetry together.
