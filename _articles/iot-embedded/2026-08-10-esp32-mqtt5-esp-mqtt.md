---
title: "MQTT 5 on ESP32 with esp-mqtt: What a Sensor Fleet Gains"
date: 2026-08-10
track: iot-embedded
summary: "What MQTT 5 adds over 3.1.1 for a fleet of ESP32 air-quality sensors — user properties, content-type, topic aliases, request/response, message expiry, shared subscriptions — and how ESP-IDF's esp-mqtt component exposes them."
reading_time: 7
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

**Gist.** Message Queuing Telemetry Transport (MQTT) version 3.1.1 carries only a topic string and an opaque payload, so a fleet of a few hundred sensor nodes must encode metadata, content type, reply addresses and staleness rules inside the topic or the payload itself. MQTT 5.0, standardised by OASIS, adds a **typed property block to every packet** — user properties, content-type, topic aliases, response topic and correlation data, message expiry — plus shared subscriptions and specific reason codes on acknowledgements. The cost is a per-packet property encoding, a broker that must negotiate limits at CONNECT time, and in ESP-IDF an application-managed property lifecycle: properties are staged for the *next* operation only, and user-property handles are heap-allocated and must be freed explicitly.

The deployment discussed here is a few hundred ESP32 air-quality nodes — SDS011 and BME680 on older boards, SPS30 on newer ones — reporting particulate matter (PM2.5), temperature, humidity and volatile organic compounds (VOC) over Wi-Fi to a backend that fans out to time-series storage and alerting. The original transport was MQTT 3.1.1.

## What MQTT 5 adds

**User properties.** MQTT 5 permits arbitrary UTF-8 key/value string pairs on most packet types, CONNECT, PUBLISH, SUBSCRIBE, the acknowledgements and DISCONNECT among them. Metadata such as firmware version, sensor model or site identifier travels as `fw=1.8.2`, `sensor=sps30`, `site=warehouse-3` rather than being encoded into the topic hierarchy or the JavaScript Object Notation (JSON) body. An ingest service can route on the properties without parsing the payload.

**Payload format and content type.** Two properties describe the payload directly: `payload_format_indicator`, which distinguishes UTF-8 text from unspecified bytes, and `content_type`, a Multipurpose Internet Mail Extensions (MIME) string such as `application/json`. A fleet mixing JSON nodes with memory-constrained nodes sending packed binary structures no longer requires the consumer to infer the encoding from the topic.

**Topic aliases.** A topic such as `saltmere/sites/warehouse-3/rack-12/node-0a4f/airquality` is 55 bytes and is retransmitted on **every** publish under 3.1.1. A topic alias is a small integer that stands in for the topic string within a single network connection. The **first publish carries both the full topic and the alias mapping; subsequent publishes carry the alias and an empty topic name**. The alias table does not survive the connection: after a reconnect the mapping must be re-established.

**Request/response.** MQTT 5 defines `response_topic` and `correlation_data` as protocol-level properties. A node publishing a firmware-check or calibration query names the topic it wants the answer on and attaches an opaque correlation token; the responder echoes the token so the requester can match reply to request. Under 3.1.1 the same pattern requires a private convention agreed between firmware and backend.

**Message expiry.** `message_expiry_interval` gives a publish a lifetime in seconds. A command queued for an offline node whose session is retained is **discarded by the broker once the interval elapses**, rather than being delivered late. For a "recalibrate now" command, late delivery is a correctness problem, not merely a latency one.

**Shared subscriptions.** A subscription to the filter `$share/{group}/{filter}` places the subscriber in a named group; the broker distributes matching messages **across the group's members instead of to all of them**. Adding an ingest worker therefore changes no firmware and requires no manual topic sharding.

**Reason codes.** MQTT 5 attaches a specific reason code to nearly every acknowledgement and to DISCONNECT — quota exceeded, packet too large, topic name invalid — where 3.1.1 offers a short list of CONNACK return codes, a single SUBACK failure value, and silence elsewhere. A node that is being rejected reports a cause rather than entering an unexplained reconnect loop.

## Enabling MQTT 5 in ESP-IDF

The functionality lives in the existing `esp-mqtt` component under ESP-IDF 5.x; the API below matches the v5.5.4 programming guide and the `mqtt5` example on `master`. Two properties of the helper family govern correct use. First, the property setters stage a bundle that applies to the **next corresponding operation only**, so they must be called immediately before the publish or subscribe they modify. Second, `esp_mqtt5_client_set_user_property` **allocates** a handle that the application must release.

```c
#include "mqtt_client.h"

static esp_mqtt_client_handle_t client;

/* User properties: an array of {key, value} string pairs. */
static esp_mqtt5_user_property_item_t pub_user_props[] = {
    {"fw",     "1.8.2"},
    {"sensor", "sps30"},
    {"site",   "warehouse-3"},
};

/* Publish-time MQTT 5 properties. */
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
        const char *topic =
            "saltmere/sites/warehouse-3/rack-12/node-0a4f/airquality";
        const char *payload = "{\"pm25\":7.4,\"voc\":112,\"t\":21.6}";

        /* Attach user properties to the publish property handle... */
        esp_mqtt5_client_set_user_property(&pub_prop.user_property,
                                           pub_user_props, 3);
        /* ...stage the whole property bundle for the next publish... */
        esp_mqtt5_client_set_publish_property(client, &pub_prop);
        /* ...then publish (QoS 1). */
        esp_mqtt_client_publish(client, topic, payload, 0, 1, 0);
        /* Free the handle allocated above; otherwise it leaks per publish. */
        esp_mqtt5_client_delete_user_property(pub_prop.user_property);
        pub_prop.user_property = NULL;

        /* Shared subscription: the broker balances across group "ingest". */
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
        .session.protocol_ver = MQTT_PROTOCOL_V_5,   /* the opt-in */
    };

    client = esp_mqtt_client_init(&cfg);
    esp_mqtt5_client_set_connect_property(client, &conn_prop);
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID,
                                   mqtt_event_handler, NULL);
    esp_mqtt_client_start(client);
}
```

Points that carry the behaviour:

- **`.session.protocol_ver = MQTT_PROTOCOL_V_5` is the entire opt-in.** Omitting it selects 3.1.1, under which the `esp_mqtt5_*` setters have no effect.
- The staged bundle is consumed by one operation. Setting properties once at startup leaves every later publish without them.
- **Topic aliases require a negotiated budget.** `topic_alias_maximum` is advertised in the connection properties, and the broker's own maximum bounds what the client may use; a zero budget makes `.topic_alias = 1` inoperative.
- **Shared subscriptions are a topic-filter convention**, `$share/{group}/{filter}`. ESP-IDF additionally exposes `is_share_subscribe` and `share_name` on the subscribe-property structure. The distribution is performed by the broker; firmware is unchanged either way.
- `maximum_packet_size` is a limit the client declares for packets it is willing to receive. The broker must not send a packet larger than that; an oversized message is dropped at the broker rather than truncated at the client.

## Reading properties on inbound messages

`MQTT_EVENT_DATA` events carry the peer's MQTT 5 properties. The pattern used by the example's `print_user_property` helper is two-stage: `esp_mqtt5_client_get_user_property_count` sizes a buffer, then `esp_mqtt5_client_get_user_property` copies the items into it. **The copied `key` and `value` strings, and the buffer itself, are heap-allocated and must be freed by the caller.** The same inbound property block carries `response_topic` and `correlation_data`, which is how a node learns where to send a reply and which token to echo.

## Migration shape

MQTT 5 and 3.1.1 clients can be served by the same broker, so a fleet migrates in waves rather than as a flag day; in the deployment described here the newest hardware moved first and older SDS011 boards remained on 3.1.1 until their next over-the-air (OTA) update. For a single node the added property machinery buys little: 3.1.1 is smaller and has no lifecycle to manage. The gains scale with fleet size — alias-compressed topics reduce airtime per publish, expiry bounds command staleness, user properties remove metadata from the payload schema, and shared subscriptions decouple ingest scaling from firmware.

The byte saving from topic aliases is deployment-specific and is best established with a packet capture before and after, since it depends on topic length, publish interval and reconnect frequency.

## Pitfalls

- Calling `esp_mqtt5_client_set_user_property` without a matching `esp_mqtt5_client_delete_user_property` leaks a heap allocation on every publish; on a node running for months the symptom is a slow decline in free heap ending in an allocation failure, not an immediate crash.
- Staging properties once during startup rather than immediately before each operation produces publishes with no properties at all, because the bundle applies to the next operation only.
- Setting `.topic_alias` without advertising `topic_alias_maximum`, or beyond the value the broker grants, sends the full topic string on every publish — the expected bandwidth reduction never appears and no error is raised.
- Topic-alias mappings are scoped to the network connection. After a reconnect, a publish that sends only the alias has no mapping at the broker; the alias must be re-registered with a full-topic publish first.
- Omitting `.session.protocol_ver = MQTT_PROTOCOL_V_5` silently yields a 3.1.1 session in which every `esp_mqtt5_*` call is inert, so properties disappear with no compile-time or runtime complaint.
- Reading inbound user properties without freeing each returned `key` and `value` in addition to the buffer leaks per received message, which is the more damaging direction on a command-subscribed node.
- `message_expiry_interval` bounds how long a broker retains an undelivered message; it does not cause an already-delivered message to be ignored, so a node must still validate command freshness itself if delivery-to-execution latency matters.
