---
title: "Scripting a print with the Moonraker API: querying and subscribing to live telemetry"
date: 2026-08-07
track: cad-3dprint
summary: "Moonraker is the HTTP/WebSocket layer that Mainsail and Fluidd talk to. The same API is open to your own scripts — query temps over REST, subscribe to a live status feed over WebSocket, and you have the raw material for a dashboard or a Prometheus exporter."
reading_time: 6
tags: [moonraker, klipper, 3d-printing, api, monitoring]
sources:
  - title: "Moonraker — Printer Administration API"
    url: "https://moonraker.readthedocs.io/en/latest/external_api/printer/"
  - title: "Moonraker — Printer Objects"
    url: "https://moonraker.readthedocs.io/en/latest/printer_objects/"
  - title: "Moonraker — API Introduction (JSON-RPC over WebSocket)"
    url: "https://moonraker.readthedocs.io/en/latest/external_api/introduction/"
  - title: "Moonraker — Authorization and Authentication"
    url: "https://moonraker.readthedocs.io/en/latest/external_api/authorization/"
  - title: "scross01/prometheus-klipper-exporter"
    url: "https://github.com/scross01/prometheus-klipper-exporter"
---

Klipper does the real-time work — stepping motors, running the PID loops — but it doesn't speak HTTP. That job belongs to **Moonraker**, the API server that sits in front of Klipper's Unix socket and exposes it to the network. When Mainsail shows you a bed temperature or Fluidd draws a progress bar, it's polling Moonraker. The useful thing for anyone who likes graphs: that same API is wide open to your own scripts. If you can write ten lines of Python, you can scrape every temperature, position, and progress figure the printer knows about.

Moonraker gives you two ways in: a **REST API** for one-shot requests, and a **JSON-RPC 2.0 API over WebSocket** for the same calls plus a live push feed. For monitoring you want both — REST to check state, the WebSocket to stream it.

## The one-shot query over REST

Start with the cheapest possible call. `GET /printer/info` tells you whether Klipper is even alive:

```bash
curl http://mainsailos.local/printer/info
```

```json
{ "state": "ready", "state_message": "Printer is ready",
  "hostname": "mainsailos", "software_version": "v0.12.0-85-gd785b396" }
```

The workhorse is `/printer/objects/query`. Klipper models the machine as a set of named **printer objects** — `heater_bed`, `extruder`, `toolhead`, `print_stats`, `display_status` — each with its own fields. You ask for the objects you care about by adding them to the query string. A bare object name returns all its fields; `object=field1,field2` narrows it down:

```bash
curl "http://mainsailos.local/printer/objects/query?extruder&heater_bed&print_stats&display_status"
```

```json
{
  "eventtime": 578243.578,
  "status": {
    "extruder":       { "temperature": 209.8, "target": 210.0, "power": 0.62 },
    "heater_bed":     { "temperature": 59.9,  "target": 60.0,  "power": 0.41 },
    "print_stats":    { "filename": "benchy.gcode", "state": "printing",
                        "print_duration": 842.3, "total_duration": 901.7 },
    "display_status": { "progress": 0.37, "message": null }
  }
}
```

That single response has everything a status widget needs. The fields worth knowing:

- **`extruder` / `heater_bed`**: `temperature` (current °C), `target` (setpoint °C), `power` (PWM, 0.0–1.0).
- **`print_stats`**: `state` (`standby`, `printing`, `paused`, `complete`, `cancelled`, `error`), `filename`, `print_duration` (seconds actually printing, excluding pauses), `total_duration` (wall-clock including pauses).
- **`display_status.progress`**: fraction complete, 0.0–1.0. (`virtual_sdcard.progress` gives file-position progress if you prefer bytes-read over the slicer's `M73` estimate.)
- **`toolhead`**: `position` (`[x, y, z, e]`) and `homed_axes` (e.g. `"xyz"`, or `""` before homing).

The REST call is a `POST` under the hood but Moonraker accepts the query string on a `GET` too, which is why the `curl` above just works. Poll it on a timer and you have a scraper — but polling is wasteful and always a little stale. The WebSocket does better.

## Subscribing to the live feed

Over the WebSocket at `/websocket`, every REST endpoint has a JSON-RPC method: `/printer/info` becomes `printer.info`, `/printer/objects/query` becomes `printer.objects.query`, and `/printer/gcode/script` becomes `printer.gcode.script`. A request is standard JSON-RPC 2.0:

```json
{ "jsonrpc": "2.0", "method": "printer.objects.query",
  "params": { "objects": { "extruder": null, "heater_bed": ["temperature", "target"] } },
  "id": 4654 }
```

`null` means "all fields"; a list narrows it. The unique-to-WebSocket call is **`printer.objects.subscribe`**, which takes the same `objects` map but then keeps pushing. After the initial reply, whenever a subscribed field changes Moonraker sends a **`notify_status_update`** notification — no `id`, params as a two-element array `[changed_objects, eventtime]`:

```json
{ "jsonrpc": "2.0", "method": "notify_status_update",
  "params": [ { "extruder": { "temperature": 210.4 } }, 578245.12 ] }
```

Crucially, updates are **deltas** — only the fields that changed appear. Your handler has to keep the last-known state and merge each update into it, not replace it. Here is the core of a logger (or the collector half of a Prometheus exporter) using the `websockets` library:

```python
import asyncio, json, websockets

URL = "ws://mainsailos.local/websocket"
SUBS = {"extruder": ["temperature", "target"],
        "heater_bed": ["temperature", "target"],
        "print_stats": ["state"],
        "display_status": ["progress"]}

async def monitor():
    state = {}  # last-known values, merged from deltas
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "printer.objects.subscribe",
            "params": {"objects": SUBS}, "id": 1}))

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("id") == 1:                 # initial full snapshot
                objs = msg["result"]["status"]
            elif msg.get("method") == "notify_status_update":
                objs = msg["params"][0]            # delta only
            else:
                continue
            for obj, fields in objs.items():
                state.setdefault(obj, {}).update(fields)

            hot = state.get("extruder", {}).get("temperature")
            bed = state.get("heater_bed", {}).get("temperature")
            pct = state.get("display_status", {}).get("progress", 0) * 100
            print(f"hotend={hot}°C  bed={bed}°C  progress={pct:.0f}%")

asyncio.run(monitor())
```

Point that at a Prometheus `Gauge` per field instead of `print()` and you've built an exporter — which is exactly what community projects like [`prometheus-klipper-exporter`](https://github.com/scross01/prometheus-klipper-exporter) do, calling `printer/objects/query` and reshaping the JSON into `/metrics`. Graph `extruder_temperature` next to `heater_bed_power` in Grafana and thermal runaway or a loose heater cartridge shows up as a shape, not a surprise.

## Sending G-code, and getting in the door

Reading is half of it. To *act* — pause on an anomaly, fire an `M117` note, kick off a print — POST to `/printer/gcode/script` (or call `printer.gcode.script`):

```bash
curl -X POST http://mainsailos.local/printer/gcode/script \
     -H 'Content-Type: application/json' -d '{"script": "M117 dashboard connected"}'
```

Whether any of this needs a credential depends on where your script runs. Moonraker's `[authorization]` block defines **`trusted_clients`** — IP addresses and subnets that skip authentication entirely. A script on the printer's own LAN subnet listed there needs nothing. From outside that range, send your **API key** in the `X-Api-Key` header on each request; you can read the current key with `GET /access/api_key`. Browser code that can't set headers uses a **oneshot token** from `/access/oneshot_token` appended as `?token=...`, but for a server-side monitor the API key is simpler. Read-only status queries are typically exempt from auth anyway — it's the G-code and file endpoints that Moonraker guards.

That's the whole loop: `printer.info` to confirm Klipper is up, `objects.subscribe` for a live delta feed, `gcode.script` to intervene. Everything Mainsail does, your dashboard can do too.

**Try next:** subscribe to `print_stats` and `display_status`, feed `progress` and the two temperatures into Prometheus gauges, and build a Grafana panel that overlays hotend temperature against `extruder.power` so a failing heater is visible before Klipper's own runaway protection trips.
