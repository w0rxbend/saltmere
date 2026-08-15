---
title: "Fluidd vs Mainsail: choosing a Klipper front-end, or running both"
date: 2026-08-13
track: cad-3dprint
summary: "Both are Vue single-page applications over the same Moonraker API, so the choice is ergonomics rather than capability. A feature comparison, the KIAUH install path, and the nginx and CORS configuration required to serve both from one host."
reading_time: 6
tags: [klipper, moonraker, mainsail, fluidd, kiauh]
sources:
  - title: "Mainsail releases (GitHub)"
    url: "https://github.com/mainsail-crew/mainsail/releases"
  - title: "Fluidd releases (GitHub)"
    url: "https://github.com/fluidd-core/fluidd/releases"
  - title: "KIAUH — Web Interfaces (Mainsail/Fluidd)"
    url: "https://github.com/dw-0/kiauh"
  - title: "Mainsail documentation"
    url: "https://docs.mainsail.xyz/"
  - title: "Fluidd documentation"
    url: "https://docs.fluidd.xyz/"
---

**Gist.** Klipper exposes no user interface of its own; control passes through Moonraker, an application programming interface (API) server, and the two dominant browser clients — Mainsail and Fluidd — are both static single-page applications (SPAs) speaking that same JSON-RPC protocol over a websocket. Because the backend is shared, feature parity is close to total and the decision reduces to layout and workflow. Running both is possible, and the cost is a second nginx server block, a second port, and a Moonraker cross-origin resource sharing (CORS) allow-list entry for every origin from which a browser will connect.

## Where the state resides

Neither front-end talks to the printer. Both are bundles of static HyperText Markup Language (HTML), JavaScript and Cascading Style Sheets (CSS) served by a web server, which open a websocket to **[Moonraker](/articles/cad-3dprint/2026-08-07-moonraker-api-printer-monitoring)** and issue JSON-RPC requests over it. The consequence is the invariant that governs everything below: **printer state, configuration files, macros and job history are owned by Klipper and Moonraker, not by the client**. A front-end holds a cached projection of that state, refreshed by Moonraker's status-update notifications. Uninstalling a client destroys nothing; installing a second one duplicates no state.

That invariant also explains the real-time mirroring described later. Two clients connected to one Moonraker instance are two subscribers to the same notification stream, so a command issued in one appears in the other on the next update, without either client knowing the other exists.

Both projects release independently and frequently; the current version of each is published on its GitHub releases page, listed in the sources below. The comparison that follows concerns capabilities that have been stable across recent releases of both, not a specific pair of version numbers.

## Feature comparison

| | Mainsail | Fluidd |
|---|---|---|
| Framework | Vue SPA, static bundle | Vue SPA, static bundle |
| Config editor | Built-in, syntax-highlighted | Built-in, syntax-highlighted |
| Layout | Column dashboard | Column dashboard |
| Macro buttons | Grouped into categories | Grouped into categories |
| Mesh view | 3D bed mesh visualiser | 3D bed mesh visualiser |
| G-code preview | Yes, per layer | Yes, per layer |
| Multi-printer | Yes (printer picker) | Yes (instances menu) |
| Theming | Custom via `.theme/` in config | Built-in colour and theme controls |
| Timelapse | moonraker-timelapse plugin | moonraker-timelapse plugin |

The differences that survive scrutiny are presentational: **the two arrange and label the same Moonraker-supplied data differently**, and no published comparison establishes a capability one has and the other lacks. Both include a built-in editor for `printer.cfg` with syntax highlighting and firmware-restart controls, so neither requires a secure shell (SSH) session for a configuration change. Since the differences are matters of layout preference, the reliable way to choose is to run both against the same printer, which the rest of this article describes.

## Installation via KIAUH

**KIAUH** (Klipper Install And Update Helper) installs Klipper, Moonraker and either front-end, and writes the web-server configuration:

```bash
cd ~ && git clone https://github.com/dw-0/kiauh.git
./kiauh/kiauh.sh
#  Install menu -> Mainsail (or Fluidd); the menu entry numbers vary by KIAUH version
```

Selecting Mainsail unpacks the static bundle to `~/mainsail`, installs an nginx server block, and adds an `[update_manager]` stanza to `moonraker.conf` so that Moonraker can update the client in place. Fluidd follows the same pattern with its own directory and its own update-manager entry.

## Serving both from one host

Because the clients are static files, coexistence is a web-server question: give each bundle its own listening port and point both at the same Moonraker instance. KIAUH offers this during installation; configured by hand, the two server blocks are structurally identical apart from the port and document root.

```nginx
# /etc/nginx/sites-available/mainsail  -> port 80
server {
    listen 80 default_server;
    root /home/pi/mainsail;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }
    # proxy /websocket, /printer, /api, /server ... to Moonraker
}

# /etc/nginx/sites-available/fluidd     -> port 81
server {
    listen 81;
    root /home/pi/fluidd;
    index index.html;
    location / { try_files $uri $uri/ /index.html; }
    # same Moonraker proxy locations
}
```

The `try_files $uri $uri/ /index.html` line is load-bearing for a single-page application: **client-side routing means paths such as `/history` exist only in the browser's router, not on disk**, so a direct request or a page reload would otherwise return HTTP 404. The fallback serves `index.html` for any path that does not resolve to a file, and the router then interprets the path.

The second load-bearing detail is CORS. **A browser treats `http://192.168.1.50` and `http://192.168.1.50:81` as distinct origins because the port differs**, so an origin that is absent from Moonraker's allow-list has its API requests rejected by the browser, not by Moonraker. The symptom is a client that loads its own assets and then shows no printer state at all.

```ini
# moonraker.conf
[authorization]
cors_domains:
    *.local
    http://192.168.1.50
    http://192.168.1.50:81
trusted_clients:
    192.168.1.0/24
```

With both blocks active, `http://printer.local` serves Mainsail and `http://printer.local:81` serves Fluidd against one printer. Each reflects commands issued from the other, which is what makes a side-by-side trial informative rather than merely possible.

## Choosing one

Switching cost is bounded by the invariant stated at the top: configuration, macros and job history are held by Klipper and Moonraker. Removing a client through KIAUH's remove menu frees its port and its document root and leaves printer state untouched, so a trial period followed by removal of the unused client costs nothing beyond disk space and one update-manager entry during the trial.

## Pitfalls

- **A front-end loads but shows no temperatures, no job and no console output.** Its origin is missing from `cors_domains`; adding the second port as a separate entry is required, since a port change makes a new origin.
- **Reloading the page on a sub-path such as `/history` returns HTTP 404.** The nginx `location /` block lacks the `try_files ... /index.html` fallback that SPA client-side routing depends on.
- **The second client is unreachable from another machine on the local network while working locally.** The extra port is not opened on the host firewall; the first client works because port 80 was already permitted.
- **Both clients appear installed, but only one updates from the interface.** Only one `[update_manager]` stanza was written to `moonraker.conf`; the client installed outside KIAUH has no update entry.
- **Changes made in one client do not appear in the other.** The two are pointed at different Moonraker instances, or one server block proxies `/websocket`, `/printer`, `/api` and `/server` to a different backend than the other.
