---
title: "MQTT 5 on ESP32 with esp-mqtt: What a Sensor Fleet Actually Gains"
date: 2026-08-10
track: iot-embedded
summary: "A practical look at why MQTT 5 beats 3.1.1 for a fleet of ESP32 air-quality sensors — user properties, content-type, topic aliases, request/response, message expiry, and shared subscriptions — with a working ESP-IDF esp-mqtt v5 example."
reading_time: 6
tags:
  - esp32
  - esp-idf
  - mqtt5
  - esp-mqtt
  - iot
  - air-quality
sources:
  - title: "ESP-MQTT — ESP-IDF Programming Guide v5.5.4"
    url: "https://docs.espressif.com/projects/esp-idf/en/v5.5.4/esp32/api-reference/protocols/mqtt.html"
  - title: "espressif/esp-idf — examples/protocols/mqtt5/main/app_main.c"
    url: "https://github.com/espressif/esp-idf/blob/master/examples/protocols/mqtt5/main/app_main.c"
  - title: "MQTT 5: Seven Reasons to Upgrade — HiveMQ MQTT 5 Essentials Part 3"
    url: "https://www.hivemq.com/blog/mqtt5-essentials-part3-upgrade-to-mqtt5-now/"
  - title: "MQTT Version 5.0 — OASIS Standard"
    url: "https://docs.oasis-open.org/mqtt/mqtt/v5.0/mqtt-v5.0.html"
  - title: "Introduction to MQTT 5.0 — EMQ"
    url: "https://www.emqx.com/en/blog/introduction-to-mqtt-5"
---

I run a couple hundred ESP32 air-quality nodes: SDS011 and BME680 up front, an SPS30 on the newer boards, all reporting PM2.5, temperature, humidity, and VOC over Wi-Fi to a backend that fans data out to time-series storage and alerting. For years that was plain MQTT 3.1.1, and it worked. But once a fleet crosses a few hundred devices, the gaps in 3.1.1 stop being academic. MQTT 5 closes most of them, and — importantly — ESP-IDF's `esp-mqtt` component has first-class support for it. This is what actually changed for me, and how to wire it up.

## Why MQTT 5, concretely

**User properties.** MQTT 3.1.1 gives you a topic and an opaque payload. That's it. Every piece of metadata — firmware version, sensor model, calibration epoch, site ID — has to be smuggled into the topic string or the JSON body. MQTT 5 adds arbitrary key/value string pairs to any packet, so I attach `fw=1.8.2`, `sensor=sps30`, `site=warehouse-3` as headers. My ingest service routes on them without parsing the payload.

**Per-message content-type.** Some nodes send JSON, the memory-tight ones send a packed binary struct. In 3.1.1 the consumer guesses from the topic. MQTT 5 carries a `content_type` (a MIME string like `application/json`) and a `payload_format_indicator` that flags UTF-8 vs. binary. The decoder just reads the header.

**Topic aliases (bandwidth).** A topic like `saltmere/sites/warehouse-3/rack-12/node-0a4f/airquality` is ~55 bytes on *every* publish. Over a battery-conscious node reporting every 10 seconds, that adds up. Topic aliases let the client register a topic once, then send a small integer in place of the full string on subsequent messages. On a chatty sensor that is real airtime and real power saved.

**Request/response.** Firmware update checks and calibration pulls are request/response, but 3.1.1 has no notion of it — you invent an ad-hoc reply-topic convention. MQTT 5 standardizes `response_topic` plus `correlation_data`, so a node can publish a query, tell the responder exactly where to reply, and match the response to its request.

**Message expiry.** A "recalibrate now" command that arrives 20 minutes late because a node was offline is worse than useless. `message_expiry_interval` tells the broker to drop a message the node never received once it goes stale — no more zombie commands.

**Shared subscriptions.** This one is backend-side but changes fleet architecture. In 3.1.1, every subscriber to `.../airquality` gets every message, so scaling ingest means manual topic sharding. MQTT 5's `$share/{group}/{topic}` load-balances messages across a group of consumers automatically. Add an ingest worker, it joins the group and takes a share.

**Better reason codes.** 3.1.1 CONNACK gives you a handful of vague return codes and a disconnect is just silence. MQTT 5 attaches specific reason codes to almost every ACK — quota exceeded, packet too large, topic name invalid — so when a node misbehaves you learn *why* instead of watching it silently reconnect-loop.

## Enabling MQTT 5 in ESP-IDF

The API lives in the same `esp-mqtt` component you already use (ESP-IDF 5.x — this was verified against the v5.5.4 programming guide and the `mqtt5` example on `master`). You flip the protocol version in the config, then use a family of `esp_mqtt5_*` helpers to stage properties before each publish/subscribe. The key thing to understand about that helper family: properties are set on the client as a **one-time, next-operation** payload. You call the setter, then immediately call the publish/subscribe, and for user properties you allocate and then free a handle around it.

```c
#include "mqtt_client.h"

static esp_mqtt_client_handle_t client;

/* User properties: an array of {key, value} string pairs. */
static esp_mqtt5_user_property_item_t pub_user_props[] = {
    {"fw",     "1.8.2"},
    {"sensor", "sps30"},
    {"site",   "warehouse-3"},
};

/* Publish-time MQTT 5 properties. content_type and topic_alias are real
   fields of esp_mqtt5_publish_property_config_t. */
static esp_mqtt5_publish_property_config_t pub_prop = {
    .payload_format_indicator = 1,          /* payload is UTF-8 text */
    .content_type             = "application/json",
    .message_expiry_interval  = 120,        /* seconds; drop if stale */
    .topic_alias              = 1,          /* reuse alias 1 for this topic */
};

static void mqtt_event_handler(void *args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;
    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED: {
        const char *topic = "saltmere/sites/warehouse-3/node-0a4f/airquality";
        const char *payload = "{\"pm25\":7.4,\"voc\":112,\"t\":21.6}";

        /* Attach user properties to the publish property handle... */
        esp_mqtt5_client_set_user_property(&pub_prop.user_property,
                                           pub_user_props, 3);
        /* ...stage the whole property bundle for the next publish... */
        esp_mqtt5_client_set_publish_property(client, &pub_prop);
        /* ...then publish (QoS 1). */
        esp_mqtt_client_publish(client, topic, payload, 0, 1, 0);
        /* Free the user-property handle we just allocated. */
        esp_mqtt5_client_delete_user_property(pub_prop.user_property);
        pub_prop.user_property = NULL;

        /* Subscribe to a shared group so backend/local consumers balance. */
        esp_mqtt_client_subscribe(client, "$share/ingest/saltmere/cmd/#", 1);
        break;
    }
    case MQTT_EVENT_DATA:
        ESP_LOGI("aq", "topic=%.*s len=%d",
                 event->topic_len, event->topic, event->data_len);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW("aq", "disconnected");
        break;
    default:
        break;
    }
}

void mqtt5_start(void)
{
    /* Connection-level MQTT 5 properties, including topic alias budget. */
    esp_mqtt5_connection_property_config_t conn_prop = {
        .session_expiry_interval = 30,
        .maximum_packet_size     = 1024,
        .topic_alias_maximum     = 8,   /* allow up to 8 aliases this session */
        .request_resp_info       = true,
    };

    esp_mqtt_client_config_t cfg = {
        .broker.address.uri = "mqtt://broker.saltmere.lan",
        .session.protocol_ver = MQTT_PROTOCOL_V_5,   /* <-- the switch */
    };

    client = esp_mqtt_client_init(&cfg);
    esp_mqtt5_client_set_connect_property(client, &conn_prop);
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(client);
}
```

A few things worth calling out, because they trip people up:

- **`.session.protocol_ver = MQTT_PROTOCOL_V_5`** is the entire opt-in. Leave it out and you get 3.1.1, and none of the `esp_mqtt5_*` setters do anything.
- The setters (`esp_mqtt5_client_set_publish_property`, `..._set_subscribe_property`) apply to the *next* corresponding call only. Set them inside the event handler, right before you publish or subscribe — not once at startup.
- `esp_mqtt5_client_set_user_property` **allocates** a handle. Always pair it with `esp_mqtt5_client_delete_user_property` after the operation, or you leak on every publish. On a device that runs for months, that matters.
- **Topic aliases** need a session budget. Advertise `topic_alias_maximum` in the connection properties; the broker must accept a non-zero value before your `.topic_alias = 1` publishes take effect. The first publish still sends the full topic *and* the alias mapping; later ones send the alias alone.
- **Shared subscriptions** are just a topic-name convention — `$share/{group}/{filter}`. ESP-IDF also exposes `is_share_subscribe` / `share_name` on the subscribe-property struct if you'd rather set it structurally. The broker handles the load-balancing; your firmware doesn't change.

## Reading properties on the way back in

Inbound `MQTT_EVENT_DATA` events carry the peer's MQTT 5 properties too. The example's `print_user_property` helper walks them: call `esp_mqtt5_client_get_user_property_count` to size a buffer, then `esp_mqtt5_client_get_user_property` to copy the items out — and **free each `key`/`value` and the buffer**, because those are heap-allocated for you. That's how a node reads the `correlation_data` and `response_topic` off an incoming request and knows where to send its reply.

## Is it worth the migration?

For a single hobby node, probably not — 3.1.1 is simpler and lighter. For a fleet, the calculus flips. Topic aliases cut airtime, message expiry kills stale commands, user properties get metadata out of your payload schema, and shared subscriptions let ingest scale horizontally without touching firmware. MQTT 5 is also backward-compatible at the broker, so you can migrate devices in waves rather than flag-day everything. I moved the newest hardware first and left the old SDS011 boards on 3.1.1 until their next OTA.

**Try next:** Wire up the request/response path end to end — publish a `calibrate?` request with a `response_topic` and `correlation_data`, have a small consumer reply, and confirm the node matches the response by correlation ID. Then measure the actual byte savings from topic aliases with a packet capture before and after.
