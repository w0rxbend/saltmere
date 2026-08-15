---
title: 'Incus: one CLI for system containers and VMs, the LXD fork that stuck'
date: 2026-08-13
track: linux-tools
summary: Incus is the community fork of LXD, run by its original maintainers under Linux Containers. It manages full-OS system containers and KVM virtual machines behind one API — a different tool from Docker, not a competitor to it.
reading_time: 5
tags:
- incus
- lxd
- containers
- virtualization
- kvm
- linux-tools
- lxc
- virtual-machines
- linux-containers
sources:
- title: Linux Containers — Incus (introduction)
  url: https://linuxcontainers.org/incus/
- title: Linux Containers — Incus news / releases
  url: https://linuxcontainers.org/incus/news/
- title: 'Incus: LXD''s Community Fork, Two Years In (SumGuy''s Ramblings)'
  url: https://sumguy.com/incus-lxd-fork/
- title: 'Incus: LXC system containers + KVM on any Linux host (Amir Eslampanah)'
  url: https://amireslampanah.com/tutorials/incus-containers-and-vms.html
- title: Install Incus on Ubuntu 26.04 / 24.04 / 22.04 (ComputingForGeeks)
  url: https://computingforgeeks.com/install-lxc-incus-ubuntu/
- title: Incus documentation — Introduction
  url: https://linuxcontainers.org/incus/docs/main/
- title: Introducing Incus — Linux Containers announcement
  url: https://linuxcontainers.org/incus/announcement/
- title: Migrating from LXD — Incus documentation
  url: https://linuxcontainers.org/incus/docs/main/howto/server_migrate_lxd/
- title: Zabbly — Incus package repository
  url: https://zabbly.com/incus/
---

When Canonical moved LXD in-house in 2023 and put it behind a CLA, the people who had built it forked it. **Incus** is that fork — Apache-2.0, hosted under the neutral **Linux Containers** project, and led by LXD's original maintainers. Two-plus years on it's the version the distros ship: it's in Debian, Ubuntu, Fedora and Arch, and it moves fast. The current line is **Incus 7.3** (released 31 July 2026) on a monthly cadence, with **7.0 LTS** for people who want stability over features.

## System containers are not application containers

This is the whole point, so get it straight before the commands. Docker runs *application* containers: one process, an ephemeral layered image, torn down and rebuilt on every deploy. Incus runs *system* containers: a full userland booting `systemd` (or OpenRC), with its own users, cron, logs and package manager — a lightweight machine you `ssh` into, not an image you rebuild. It uses LXC underneath, so there's no second kernel and near-zero overhead.

| Axis          | Docker (app container)   | Incus (system container)     |
|---------------|--------------------------|------------------------------|
| Runs          | one process              | full init + many services    |
| Lifecycle     | immutable, rebuilt       | long-lived, mutable          |
| Feels like    | a packaged binary        | a small VM                   |
| Also does VMs | no                       | yes, same CLI (`--vm`)       |

The last row matters: the same `incus` command that launches a container launches a **KVM virtual machine**. When you need a different kernel or hard isolation, you add one flag — not a new tool.

## The five commands you'll actually use

```bash
# one-time setup
incus admin init --minimal

# launch a system container from the image server
incus launch images:ubuntu/24.04 web1

# launch the same OS as a full VM instead
incus launch images:ubuntu/24.04 buildvm --vm

incus list                         # instances, state, IPs
incus exec web1 -- bash            # a shell inside it
incus shell web1                   # same thing, shorthand
incus stop web1 && incus delete web1
```

`incus exec` runs any command in the instance; pair it with `--` so flags go to the guest, not to Incus. Files move with `incus file push ./app.conf web1/etc/app.conf`.

## Profiles: configuration you attach, not repeat

A profile is a reusable bundle of instance config and devices. Every instance gets the `default` profile; you compose more on top. Define one once and stamp it across many instances:

```bash
incus profile show default            # inspect what you start with
incus profile create web
incus profile edit web                # opens the YAML below
```

```yaml
config:
  limits.cpu: "2"
  limits.memory: 2GiB
  boot.autostart: "true"
devices:
  eth0:
    name: eth0
    network: incusbr0
    type: nic
  http:
    connect: tcp:127.0.0.1:8080
    listen: tcp:0.0.0.0:80
    type: proxy
```

```bash
incus launch images:debian/12 web2 --profile default --profile web
```

That `web2` now has 2 CPUs, a 2 GiB cap, autostart, and a proxy device forwarding host port 80 to the container's 8080 — all from the profile, nothing repeated on the command line. Change the profile and every instance using it picks it up.

## Snapshots, images, and one line on clustering

Instances snapshot and clone cheaply, which makes them a good base for reproducible dev environments:

```bash
incus snapshot create web1 clean
incus publish web1/clean --alias web-base   # freeze it into an image
incus launch web-base web3                    # stamp new instances from it
```

**Clustering:** `incus cluster` joins many hosts into one logical Incus that schedules instances across nodes and shares images — the same CLI and API, now spanning a fleet, with no external control plane.

Incus isn't a Docker replacement; reach for it when you want machines rather than packaged processes — CI runners, per-project dev boxes, or a homelab where containers and VMs live behind one command.

**Try next:** `incus launch images:ubuntu/24.04 lab --vm`, then `incus launch images:ubuntu/24.04 lab-c` — put a VM and a container side by side, `incus list` them, and feel how little the VM flag costs you.

## Where it is in 2026

Incus does yearly LTS lines plus a monthly feature stream. As of August 2026 the current feature release is **Incus 7.1** (released May 29, 2026), and the supported LTS is the **7.0 series** (7.0.0, May 2026), with the older 6.0 LTS still receiving fixes. On packaging: Incus is in Debian (since Debian 13) and available on Ubuntu, and for the newest builds the project points at the **Zabbly** repositories maintained by Stéphane Graber, which ship both the stable and LTS channels for Debian and Ubuntu.

Install from Zabbly on a Debian/Ubuntu host:

```sh
curl -fsSL https://pkgs.zabbly.com/key.asc | sudo tee /etc/apt/keyrings/zabbly.asc
sudo sh -c 'cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources' <<'EOF'
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: $(. /etc/os-release && echo $VERSION_CODENAME)
Components: main
Signed-By: /etc/apt/keyrings/zabbly.asc
EOF
sudo apt update && sudo apt install -y incus
sudo incus admin init --minimal      # storage pool + default network, no prompts
```

## Migrating off LXD

You don't dump and re-import. Incus ships **`lxd-to-incus`**, an official one-shot migrator that reads your running LXD's instances, profiles, storage pools, and networks and moves them over in place, leaving LXD stopped afterward:

```sh
sudo lxd-to-incus          # interactive; verifies both daemons, then transfers state
```

It refuses to run on configurations it can't translate cleanly, so it fails loudly rather than silently dropping data — read its preflight output before confirming.

**Try next:** On a spare Debian/Ubuntu box, install Incus from Zabbly, `incus launch images:debian/13 t1` and `incus launch images:debian/13 t2 --vm`, then `incus list` and compare how fast each reaches `RUNNING`.
