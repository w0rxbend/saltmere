---
title: "Quadlets: Containers as systemd Units"
date: 2026-08-15
track: linux-tools
summary: "Drop a 10-line .container file into /etc/containers/systemd/ and a systemd generator turns it into a full service unit at daemon-reload — no hand-written ExecStart=podman run, no docker-compose daemon. Quadlet has been in Podman since 4.4 (February 2023) and now covers .container, .pod, .volume, .network, .kube, .image, .build, and .artifact units; Podman 6.1.0 (August 2026) adds more search paths and a podman quadlet subcommand family. Here's the full workflow, including rootless units, auto-updates, and health-gated startup."
reading_time: 6
tags: [podman, systemd, quadlet, containers, auto-update]
sources:
  - title: "podman-systemd.unit(5) — Podman documentation"
    url: "https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html"
  - title: "Make systemd better for Podman with Quadlet — Red Hat blog"
    url: "https://www.redhat.com/en/blog/quadlet-podman"
  - title: "Podman release notes (containers/podman)"
    url: "https://github.com/containers/podman/blob/main/RELEASE_NOTES.md"
  - title: "Quadlet, an easier way to run system containers — Alexander Larsson"
    url: "https://blogs.gnome.org/alexl/2021/10/12/quadlet-an-easier-way-to-run-system-containers/"
---

The traditional way to run a container under systemd is a unit file with a long `ExecStart=/usr/bin/podman run --rm --name web -p 8080:80 ...` line, plus `ExecStop=`, plus a prayer that the container name never collides with a leftover. It works, but you are maintaining shell-flag soup inside INI syntax, and systemd has no idea what any of it means. **Quadlet** — merged into Podman 4.4 in February 2023, originally a standalone tool by Alexander Larsson — flips this around: you write a small declarative file describing the container, and a **systemd generator** expands it into a proper transient service unit every time systemd reloads. Podman is at **6.1.0** (released August 12, 2026; the 6.0.0 major in June dropped cgroup v1, CNI, and iptables support entirely), and Quadlet is now the project's blessed answer to "how do I run this at boot" — `podman generate systemd` has been deprecated in its favor since 4.7.

## One file, one service

A minimal web server, written to `/etc/containers/systemd/web.container`:

```ini
[Unit]
Description=Web frontend

[Container]
Image=quay.io/example/web:1.4
PublishPort=8080:80
Volume=web-data.volume:/var/lib/web
Network=app.network
Environment=LOG_LEVEL=info
AutoUpdate=registry
HealthCmd=curl -fsS http://localhost:80/healthz
HealthInterval=30s
Notify=healthy

[Service]
Restart=always

[Install]
WantedBy=multi-user.target
```

After `systemctl daemon-reload`, a `web.service` exists — `systemctl start web` runs the container, `journalctl -u web` shows its logs. The generator lives at `/usr/lib/systemd/system-generators/podman-system-generator`; you can see exactly what it produces with the `--dryrun` flag. Note the `[Install]` section: generated units are **not** enabled by default, and only `WantedBy=`, `RequiredBy=`, `UpheldBy=`, and `Alias=` are honored there.

The `Volume=web-data.volume:...` and `Network=app.network` references point at sibling **`.volume`** and **`.network`** files; Quadlet wires up `Requires=`/`After=` dependencies so the named volume and network exist before the container starts. The full unit-type roster as of Podman 6.x: `.container`, `.pod` (since 5.0), `.volume`, `.network`, `.kube` (runs a Kubernetes YAML via `podman kube play`), `.image` (pre-pulls an image), `.build` (builds from a Containerfile, since 5.2), and `.artifact` (OCI artifacts). Generated service types default to `notify` for containers and kube units, `oneshot` for volumes, networks, images, and builds. Quadlet requires cgroup v2 — which, since Podman 6.0, is the only mode Podman supports anyway.

## Rootless: the same thing in your home directory

Everything above works per-user. Put the same file in `~/.config/containers/systemd/` (or `/etc/containers/systemd/users/${UID}/` if root manages units for users; Podman 6.0 added `/usr/share/containers/systemd/users/` for distro-shipped ones), then:

```bash
systemctl --user daemon-reload
systemctl --user start web
loginctl enable-linger $USER    # keep user services running after logout
```

Rootless quadlets get an automatic dependency on `podman-user-wait-network-online.service` instead of `network-online.target`, because user managers can't see system network state. This is the cleanest rootless-container-at-boot story Linux has ever had: no cron `@reboot` hacks, no root daemon, real `Restart=` semantics.

## Auto-update and health-gated startup

`AutoUpdate=registry` in the `[Container]` section sets the `io.containers.autoupdate=registry` label on the container. The stock **`podman-auto-update.timer`** (daily by default) then runs `podman auto-update`: for each labeled container it compares the registry digest of `Image=`, pulls if newer, and restarts the systemd service. The killer feature is **rollback** — because the container reports readiness over `sd_notify`, a new image that fails to come up healthy causes the update to be rolled back to the previous image automatically.

That readiness signal is the `Notify=` line. `Notify=healthy` (Podman 4.9+) tells Podman to send `READY=1` only once the container's **healthcheck** (`HealthCmd=`) first passes, so `systemctl start` blocks — and dependent units wait — until the app is actually serving, not merely forked. Set `TimeoutStartSec=` generously; a slow first healthcheck otherwise gets your service killed at startup.

## How it compares

| | Quadlet | docker-compose | ExecStart=podman run |
|---|---|---|---|
| Supervisor | systemd (PID 1) | compose/dockerd | systemd, blindly |
| Boot integration | native, incl. rootless | needs a wrapper unit | native |
| Dependencies | real unit deps (`After=`, `Wants=`, volumes/networks auto-wired) | `depends_on` (start order only) | hand-written |
| Resource control | full cgroup v2 via `[Service]` (`MemoryMax=`, slices) | compose-file limits | full, but manual |
| Auto-update + rollback | built in (`AutoUpdate=` + sd-notify) | external tools (watchtower) | manual |
| Multi-container app | `.pod` unit or shared `.network` | first-class | painful |
| Drop-in overrides | `web.container.d/*.conf` | override files | unit drop-ins |
| Syntax churn risk | generator updated with Podman | compose spec | flags rot in your unit |

The last row is Larsson's original argument: when Podman changes its flags or defaults, the generator is updated in lockstep, so your `.container` files keep working; a hand-rolled `ExecStart=` line is frozen the day you write it. Compose still wins for dev-loop ergonomics (`up`, `down`, one YAML for twelve services); Quadlet wins the moment the target is a server you manage with systemd — the same place you already handle secrets via [systemd-creds](/articles/linux-tools/2026-08-15-systemd-creds-tpm-secrets) and resource limits via slices.

## Debugging the generator

Quadlet failures are silent by design (generators can't block boot), which is the main operational gotcha. Two tools:

```bash
# See what the generator emits, with errors on stderr
/usr/lib/systemd/system-generators/podman-system-generator --dryrun
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun

# Podman 6.x: list installed quadlets and their status
podman quadlet list
```

A typo'd key is an error (the unit disappears); an entirely misnamed file is skipped. `systemd-analyze verify web.service` catches the rest. Also remember templates work — `backup@.container` gives you `backup@.service` instances — and drop-in directories (`web.container.d/50-limits.conf`) let config management layer changes without touching the base file.

Quadlets pair naturally with image-based hosts: a [bootc](/articles/linux-tools/2026-07-31-bootc-bootable-containers) system ships its OS as an OCI image and its workload as `.container` files baked into `/usr/share/containers/systemd/`, so the entire machine — host and apps — updates and rolls back through registries.

**Try next:** take one container you currently start by hand, write it as a rootless `~/.config/containers/systemd/app.container` with `HealthCmd=`, `Notify=healthy`, and `AutoUpdate=registry`, run the generator with `--user --dryrun` to inspect the generated service, then `loginctl enable-linger` and reboot to confirm it comes up without you.
