---
title: "Quadlets: Containers as systemd Units"
date: 2026-08-15
track: linux-tools
summary: "A ten-line .container file placed in /etc/containers/systemd/ is expanded by a systemd generator into a full service unit at daemon-reload, replacing a hand-written ExecStart=podman run line and needing no compose daemon. Quadlet has shipped in Podman since 4.4 (February 2023) and now covers .container, .pod, .volume, .network, .kube, .image, .build and .artifact units; Podman 6.x adds further search paths and a podman quadlet subcommand family. The workflow covered here includes rootless units, auto-update with rollback, and health-gated startup."
reading_time: 7
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

**Gist.** Running a container under systemd conventionally means encoding an entire `podman run` command line inside an INI-format unit, which leaves systemd with no model of the container beyond an opaque process to supervise. **Quadlet** — merged into Podman 4.4 in February 2023, originally a standalone tool by Alexander Larsson — replaces that line with a declarative unit file which a **systemd generator** expands into a real service unit on every daemon reload. The cost is indirection: the executed unit is a generated artefact that exists only in memory, so failures surface as a missing service rather than an error message, and diagnosis requires running the generator by hand.

## The generator contract

A generator is a binary systemd executes early in every reload, before any unit is started, whose only output is unit files written into a transient directory. Quadlet's generator is installed at `/usr/lib/systemd/system-generators/podman-system-generator`. It reads unit files whose extension names a Quadlet type, and emits a corresponding `.service`. The invariant that governs everything downstream is that **generators cannot fail the boot**: a generator that errors is ignored, its output absent, and the system continues. This is why a malformed `.container` file yields no `web.service` at all rather than a failed one.

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

After `systemctl daemon-reload` a `web.service` exists; `systemctl start web` runs the container and `journalctl -u web` shows its logs. The `[Unit]` and `[Service]` sections are passed through to the generated unit unchanged, which is what makes ordinary systemd resource control (`MemoryMax=`, slices) available without Quadlet needing a key for each knob. The `[Install]` section is treated more narrowly: generated units are **not enabled by default**, and only `WantedBy=`, `RequiredBy=`, `UpheldBy=` and `Alias=` are honoured there.

## Cross-unit references

`Volume=web-data.volume:/var/lib/web` and `Network=app.network` name sibling Quadlet files rather than existing Podman objects. The generator resolves those names to the service units it generates for them and emits `Requires=` and `After=` dependencies, so the named volume and network are created before the container starts. This is the structural difference from a compose file's `depends_on`, which expresses start order without making the dependency a supervised unit.

The unit-type roster as of Podman 6.x is `.container`, `.pod` (since 5.0), `.volume`, `.network`, `.kube` (runs a Kubernetes YAML file through `podman kube play`), `.image` (pre-pulls an image), `.build` (builds from a Containerfile) and `.artifact` (Open Container Initiative artifacts). Generated units default to service type `notify` for container and kube units, and `oneshot` for volumes, networks, images and builds.

## Rootless placement

The same file works per-user from `~/.config/containers/systemd/`, or from `/etc/containers/systemd/users/${UID}/` when root manages units on a user's behalf. Distribution-shipped units live under `/usr/share/containers/systemd/`, which has a `users/` subdirectory for the rootless case.

```bash
systemctl --user daemon-reload
systemctl --user start web
loginctl enable-linger $USER    # keep user services running after logout
```

Rootless quadlets receive an automatic dependency on **`podman-user-wait-network-online.service`** rather than on `network-online.target`, which is a system-manager unit and so not orderable from a user manager. Without `enable-linger`, the user manager is torn down at logout and the container with it, so a unit that starts correctly in an interactive session will not survive to the next boot.

## Auto-update and the rollback path

`AutoUpdate=registry` sets the label `io.containers.autoupdate=registry` on the container. The shipped **`podman-auto-update.timer`**, daily by default, runs `podman auto-update`, which for each labelled container compares the registry digest of the configured `Image=` against the running one, pulls when it differs, and restarts the service.

The state machine that makes this safe has three steps: restart with the new image, wait for the service to reach the active state, and — if it does not — **roll back to the previous image and restart again**. The readiness edge that drives the second step comes from `sd_notify`: the container process, or Podman on its behalf, sends `READY=1`. `Notify=healthy` delays that signal until the container's healthcheck (`HealthCmd=`) first passes, so `systemctl start` and every unit ordered after it block until the application answers rather than merely until it forked. **A container with no readiness signal reaches active immediately, so a broken new image looks like a successful update** and no rollback occurs.

## Comparison

| | Quadlet | docker-compose | ExecStart=podman run |
|---|---|---|---|
| Supervisor | systemd (PID 1) | compose/dockerd | systemd, blindly |
| Boot integration | native, incl. rootless | needs a wrapper unit | native |
| Dependencies | real unit deps (`After=`, `Wants=`, volumes/networks auto-wired) | `depends_on` (start order only) | hand-written |
| Resource control | full cgroup v2 via `[Service]` (`MemoryMax=`, slices) | compose-file limits | full, but manual |
| Auto-update + rollback | built in (`AutoUpdate=` + sd-notify) | external tools (watchtower) | manual |
| Multi-container app | `.pod` unit or shared `.network` | first-class | painful |
| Drop-in overrides | `web.container.d/*.conf` | override files | unit drop-ins |
| Syntax churn risk | generator updated with Podman | compose spec | flags rot in the unit |

The final row follows the motivation given in Larsson's 2021 post introducing the tool: the generator ships with Podman and is updated alongside it, so a declarative `.container` file is translated by code of the same vintage as the runtime, whereas a hand-written `ExecStart=` line is fixed at the moment of writing. `podman generate systemd`, which produced such fixed units, is deprecated in favour of Quadlet. Compose retains an advantage in development-loop ergonomics — `up`, `down`, one file for many services — while Quadlet applies where the target is a systemd-managed server, alongside secret delivery via [systemd-creds](/articles/linux-tools/2026-08-15-systemd-creds-tpm-secrets) and slice-based limits.

## Inspecting the generated output

Because generator errors do not reach the journal in the ordinary way, the diagnostic move is to run the generator directly and read its standard error:

```bash
# See what the generator emits, with errors on stderr
/usr/lib/systemd/system-generators/podman-system-generator --dryrun
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun

# Podman 6.x: list installed quadlets and their status
podman quadlet list
```

`systemd-analyze verify web.service` checks the generated unit itself. Templates behave as elsewhere in systemd: `backup@.container` produces `backup@.service` instances. Drop-in directories such as `web.container.d/50-limits.conf` allow configuration management to layer changes without editing the base file.

Quadlets compose with image-based hosts: a [bootc](/articles/linux-tools/2026-07-31-bootc-bootable-containers) system ships its operating system as an OCI image and its workload as `.container` files placed in `/usr/share/containers/systemd/`, so host and applications both update and roll back through registries.

## Pitfalls

- **A mistyped key removes the whole service.** The generator treats an unrecognised key as an error and emits nothing for that file, so the symptom is `Unit web.service not found` rather than a parse warning. `--dryrun` prints the error.
- **A misnamed file is silently skipped.** A file whose extension is not a Quadlet type is not an error and produces no output and no message, so `web.containers` or `web.conf` fails identically to a file that was never created.
- **Editing a `.container` file changes nothing until `daemon-reload`.** The running service continues from the previously generated unit, so a change that appears applied is not.
- **A slow first healthcheck kills the service at startup.** With `Notify=healthy`, `READY=1` is withheld until the healthcheck passes; if that exceeds `TimeoutStartSec=`, systemd treats the start as failed and terminates the container.
- **Auto-update without a readiness signal cannot roll back.** Rollback is triggered by the restarted service failing to become active; a container that reports ready before it can serve traffic makes a broken image indistinguishable from a working one.
- **Rootless units die at logout without `loginctl enable-linger`.** The user manager exits with the last session, taking its containers with it, so the unit works when tested interactively and is absent after reboot.
- **`[Install]` in a Quadlet file does not enable the unit.** Generated units cannot be enabled with `systemctl enable`; only the listed directives (`WantedBy=`, `RequiredBy=`, `UpheldBy=`, `Alias=`) take effect, and they do so through the generated unit itself.
