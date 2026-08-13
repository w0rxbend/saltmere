---
title: "Fluidd vs Mainsail: picking a Klipper front-end (and running both)"
date: 2026-08-13
track: cad-3dprint
summary: "Both are Vue front-ends over the same Moonraker API, so the choice is ergonomics, not capability. Here's a straight feature comparison at current versions, plus the KIAUH install and how to run them side by side on one Pi."
reading_time: 5
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

Mainsail and Fluidd are the two dominant web UIs for Klipper. Neither talks to the printer directly — they're static single-page apps that speak to **[Moonraker](/articles/cad-3dprint/2026-08-07-moonraker-api-printer-monitoring)** over its JSON-RPC/websocket API. Because they share that one backend, anything one can do (upload G-code, edit config, run macros, mesh, timelapse) the other can too. The decision is purely about layout and workflow feel. Current releases as of this writing: **Mainsail v2.18.2** (2026-07-05) and **Fluidd v1.37.4** (2026-08-11) — both actively shipping.

## Feature comparison

| | Mainsail | Fluidd |
|---|---|---|
| Framework | Vue 3 SPA | Vue 3 SPA |
| Config editor | Built-in, Ace, section-aware | Built-in, Ace, section-aware |
| Layout | Fixed panel columns, opinionated | Fully drag-to-rearrange dashboard |
| Macro buttons | Grouped, color/emoji support | Grouped, inline param prompts |
| Mesh view | 3D bed mesh visualiser | 3D bed mesh visualiser |
| G-code preview | Yes, per-layer | Yes, per-layer |
| Multi-printer | Yes (printer picker) | Yes (instances menu) |
| Theming | Custom via `.theme/` in config | Built-in color/theme controls |
| Timelapse | moonraker-timelapse plugin | moonraker-timelapse plugin |
| Feel | Guided, tidy defaults | Denser, more configurable |

The honest summary: **Mainsail** hands you a clean, opinionated layout that's great out of the box and consistent across machines — nice for teaching or a print farm. **Fluidd** lets you drag panels into whatever dashboard you want and packs more onto each screen — nice if you live in the UI and have preferences. Both edit `printer.cfg` in-browser with syntax highlighting and reload/restart buttons, so neither forces you to SSH for a quick tweak.

## Install with KIAUH

Nobody should install these by hand. **KIAUH** (Klipper Install And Update Helper) does Klipper, Moonraker, and the front-ends, and wires up the reverse proxy for you:

```bash
cd ~ && git clone https://github.com/dw-0/kiauh.git
./kiauh/kiauh.sh
#  [1] Install  ->  [4] Mainsail   (or [5] Fluidd)
```

Picking Mainsail installs the static bundle to `~/mainsail`, drops an nginx server block, and adds the `[update_manager]` stanza to `moonraker.conf` so Moonraker can update the UI in-place. Fluidd is identical with its own directory and update entry.

## Running both at once

You do not have to choose — they're just static files behind nginx, so serve each on its own port. KIAUH offers this during install; if you wire it manually, the two server blocks look like:

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

Then add both origins to Moonraker's CORS allow-list so the browser doesn't block the API:

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

Hit `http://printer.local` for Mainsail and `http://printer.local:81` for Fluidd. Same printer, same state, two windows — genuinely useful when deciding which to keep, since both reflect every command the other sends in real time.

## Which to keep

There's no wrong answer and no lock-in: your printer's brain is [Klipper + Moonraker](/articles/cad-3dprint/2026-08-07-moonraker-api-printer-monitoring), and the UI is a swappable skin. Run both for a week, then uninstall the loser in KIAUH (`[6] Remove`) to reclaim the port. Config, macros, and history live in Moonraker, so nothing is lost either way.

**Try next:** clone KIAUH, install both Mainsail on :80 and Fluidd on :81, add the second origin to `cors_domains`, and drive one print from each in split-screen — you'll know your pick within a layer or two.
