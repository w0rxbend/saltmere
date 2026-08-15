---
title: "Scripting a print with the Moonraker API: querying and subscribing to live telemetry"
date: 2026-08-07
track: cad-3dprint
summary: "Moonraker is the HTTP/WebSocket layer that Mainsail and Fluidd talk to. The same API is open to external scripts — temperatures over REST, a live delta-encoded status feed over WebSocket, and the raw material for a dashboard or a Prometheus exporter."
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

**Gist.** Klipper performs the real-time work — stepping motors, running the proportional-integral-derivative (PID) heater loops — and communicates over a Unix domain socket rather than over the network. Moonraker is the API server that fronts that socket and exposes printer state through a representational state transfer (REST) interface and a JSON-RPC 2.0 interface over WebSocket, the latter offering a push subscription. The cost of the push path is that **updates are deltas**: the client, not the server, is responsible for holding last-known state and merging each notification into it, and a client that treats a notification as a full snapshot will lose every field that did not change.

## Printer objects and the one-shot query

Klipper models the machine as a set of named **printer objects** — `heater_bed`, `extruder`, `toolhead`, `print_stats`, `display_status` — each carrying its own field set. Every read path in the API, one-shot or streaming, addresses state through this same namespace.

The cheapest call reports whether Klipper is running at all:

```bash
curl http://mainsailos.local/printer/info
```

```json
{ "state": "ready", "state_message": "Printer is ready",
  "hostname": "mainsailos", "software_version": "v0.12.0-85-gd785b396" }
```

`/printer/objects/query` is the general read. Objects are named in the query string; a bare object name returns all of its fields, and `object=field1,field2` narrows the response to the named fields:

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

The fields that carry the monitoring signal:

- **`extruder` / `heater_bed`**: `temperature` (current, °C), `target` (setpoint, °C), `power` (pulse-width modulation duty cycle, 0.0–1.0).
- **`print_stats.state`**: one of `standby`, `printing`, `paused`, `complete`, `cancelled`, `error`. This is the print state machine as Klipper reports it; a monitor that derives "is printing" from a non-zero progress value will misclassify a paused job.
- **`print_stats.print_duration` versus `total_duration`**: the first counts seconds spent printing and **excludes pauses**, the second is wall-clock and includes them. Remaining-time estimates built on the wrong one drift by exactly the accumulated pause time.
- **`display_status.progress`**: fraction complete, 0.0–1.0. A slicer that emits `M73` sets this value directly. `virtual_sdcard.progress` reports file-position progress instead, which tracks how far through the file the reader has advanced rather than an estimate of remaining work.
- **`toolhead`**: `position` as `[x, y, z, e]`, and `homed_axes` — `"xyz"` when all three are homed, `""` before homing. Position values are meaningless as absolute machine coordinates while `homed_axes` is empty.

Arguments travel in the query string rather than in a request body, which is why the `curl` above is a bare `GET`. Polling this endpoint on a timer yields a scraper whose staleness is bounded by the poll interval, and whose request volume is independent of the rate at which state changes.

## The subscription and its delta invariant

At `/websocket`, each REST endpoint has a JSON-RPC method counterpart: `/printer/info` becomes `printer.info`, `/printer/objects/query` becomes `printer.objects.query`, `/printer/gcode/script` becomes `printer.gcode.script`. Requests are ordinary JSON-RPC 2.0:

```json
{ "jsonrpc": "2.0", "method": "printer.objects.query",
  "params": { "objects": { "extruder": null, "heater_bed": ["temperature", "target"] } },
  "id": 4654 }
```

`null` requests all fields of an object; a list restricts it.

**`printer.objects.subscribe`** takes the same `objects` map and changes the interaction shape. Its immediate reply is a **full snapshot** of every subscribed field, identified by the request `id`. Thereafter Moonraker pushes **`notify_status_update`** notifications — no `id`, and `params` is a two-element array of `[changed_objects, eventtime]`:

```json
{ "jsonrpc": "2.0", "method": "notify_status_update",
  "params": [ { "extruder": { "temperature": 210.4 } }, 578245.12 ] }
```

The invariant the client must maintain: **the snapshot establishes the initial state, and every subsequent notification is a partial update applied over it**. Only changed fields appear. A handler that assigns `state = params[0]` instead of merging will report `heater_bed.temperature` as absent for as long as the bed sits at setpoint, because a field at steady state generates no update at all. The distinction between the two message shapes is structural, not semantic: the snapshot arrives under `result.status` with the matching `id`, the delta under `params[0]` with `method` set.

The example below implements the merge. It is the collector half of an exporter, using the `websockets` library:

```python
import asyncio, json, websockets

URL = "ws://mainsailos.local/websocket"
SUBS = {"extruder": ["temperature", "target"],
        "heater_bed": ["temperature", "target"],
        "print_stats": ["state"],
        "display_status": ["progress"]}

async def monitor():
    state = {}  # last-known values; deltas are merged in, never assigned over
    async with websockets.connect(URL) as ws:
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "method": "printer.objects.subscribe",
            "params": {"objects": SUBS}, "id": 1}))

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("id") == 1:                 # initial full snapshot
                objs = msg["result"]["status"]
            elif msg.get("method") == "notify_status_update":
                objs = msg["params"][0]            # changed fields only
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

Substituting a Prometheus `Gauge` per field for the `print()` produces an exporter. [`prometheus-klipper-exporter`](https://github.com/scross01/prometheus-klipper-exporter) takes the polling variant of the same approach, calling `printer/objects/query` and reshaping the JSON into a `/metrics` response. Plotting `temperature` against `power` for one heater makes a divergence between duty cycle and achieved temperature — the signature of a failing heater cartridge or a detached thermistor — visible as a trend rather than as a single alarm event.

## Writing G-code, and the authorization paths

The write endpoint is `/printer/gcode/script`, or `printer.gcode.script` over the socket:

```bash
curl -X POST http://mainsailos.local/printer/gcode/script \
     -H 'Content-Type: application/json' -d '{"script": "M117 dashboard connected"}'
```

Whether a credential is required depends on the caller's address. Moonraker's `[authorization]` block defines **`trusted_clients`**, a set of addresses and subnets exempt from authentication; a script running from a listed address sends no credential. Outside that range, requests carry an **API key** in the `X-Api-Key` header, and `GET /access/api_key` returns the current key. Browser contexts that cannot set headers use a **oneshot token** obtained from `/access/oneshot_token` and appended as `?token=...`. Moonraker's authorization documentation exempts a set of endpoints from authentication; the G-code and file endpoints are among those it guards.

## Pitfalls

- **Assigning a `notify_status_update` payload over the state dictionary instead of merging it.** Symptom: fields disappear from the dashboard while the machine is at steady state. Cause: a field that has not changed since the last notification is omitted from the delta, so the assignment erases it.
- **Deriving print activity from `display_status.progress`.** Symptom: a paused job is reported as printing. Cause: `progress` retains its last value across a pause; only `print_stats.state` distinguishes `printing` from `paused`.
- **Computing time-remaining from `total_duration`.** Symptom: the estimate is inflated by exactly the time spent paused. Cause: `total_duration` is wall-clock, whereas `print_duration` excludes pause intervals.
- **Reading `toolhead.position` before homing.** Symptom: coordinates that do not correspond to any physical location. Cause: `homed_axes` is `""` until each axis is homed, and position is not referenced to the machine origin until then.
- **Testing a script from the printer's own subnet and deploying it elsewhere.** Symptom: requests that worked in development return an authorization error in production. Cause: `trusted_clients` exempted the development address, so the missing `X-Api-Key` header went unnoticed.
- **Subscribing to a narrowed field list and later reading a field outside it.** Symptom: the field is permanently absent from merged state. Cause: `printer.objects.subscribe` pushes only the fields named in the `objects` map; a field not subscribed is never sent, snapshot or delta.
