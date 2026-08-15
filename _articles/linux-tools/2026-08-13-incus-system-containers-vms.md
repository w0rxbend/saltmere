---
title: 'Incus: one CLI for system containers and VMs, the LXD fork that stuck'
date: 2026-08-13
track: linux-tools
summary: Incus is the community fork of LXD, run by its original maintainers under Linux Containers. It manages full-OS system containers and KVM virtual machines behind one API — a different tool from Docker, not a competitor to it.
reading_time: 6
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

**Gist.** Workloads that need a whole operating system — an init system, users, cron, a package manager — do not fit the one-process, immutable-image model that application-container tooling assumes. Incus, the Apache-2.0 fork of LXD maintained under the Linux Containers project after Canonical moved LXD in-house in 2023 and placed it behind a contributor licence agreement (CLA), exposes **system containers built on LXC and full KVM virtual machines through a single command-line interface and REST application programming interface (API)**. The cost is that instances become long-lived mutable state: they are not rebuilt from a Dockerfile on every deploy, so their drift, snapshots and storage pools must be managed as machines rather than as artefacts.

## System containers are not application containers

The distinction governs everything that follows. An *application* container runs one process from a layered image and is torn down and recreated on each deployment. A *system* container runs **a complete userland whose PID 1 is an init system — `systemd` or OpenRC — with its own user database, scheduled jobs, log files and package manager**. Incus drives these through LXC, so the guest **shares the host kernel**: there is no second kernel to boot and no hardware emulation layer.

| Axis          | Docker (app container)   | Incus (system container)     |
|---------------|--------------------------|------------------------------|
| Runs          | one process              | full init + many services    |
| Lifecycle     | immutable, rebuilt       | long-lived, mutable          |
| Feels like    | a packaged binary        | a small VM                   |
| Also does VMs | no                       | yes, same CLI (`--vm`)       |

The final row carries the practical weight. **The same `incus launch` invocation that creates a container creates a KVM virtual machine when `--vm` is appended.** A virtual machine boots its own kernel and is isolated by the hypervisor rather than by namespaces and control groups, which is the configuration required when the workload needs a different kernel version, a kernel module the host does not provide, or a stronger isolation boundary. Moving between the two changes one flag, not the tooling.

## The core command set

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

`incus exec` executes an arbitrary command inside the instance. **The `--` separator is load-bearing: without it, flags intended for the guest command are parsed by the Incus client instead.** File transfer is explicit — `incus file push ./app.conf web1/etc/app.conf` — because there is no build context to copy from.

`incus list` reports instance state, and the state machine is that of a machine rather than of a process: an instance is `STOPPED`, `RUNNING` or `FROZEN`, and `incus delete` refuses a running instance unless it is stopped first.

## Profiles: configuration attached rather than repeated

A profile is a named, reusable bundle of instance configuration keys and device definitions. **An instance created without an explicit profile list receives the `default` profile; when a list is given, the profiles compose in the order stated**, so a later profile's key overrides an earlier one's.

```bash
incus profile show default            # the configuration applied by default
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

The resulting `web2` has two CPUs, a 2 GiB memory cap, autostart at daemon start, a bridged network interface on `incusbr0`, and a **proxy device that listens on host port 80 and forwards to port 8080 inside the instance**. None of this is repeated per instance. **Editing the profile propagates to every instance that references it**, which is the reason profile changes are a fleet-wide operation and must be treated as such.

## Snapshots, images and clustering

Instances snapshot and clone through the storage pool, which makes them a usable base for reproducible development environments:

```bash
incus snapshot create web1 clean
incus publish web1/clean --alias web-base   # freeze it into an image
incus launch web-base web3                    # stamp new instances from it
```

`incus publish` converts a snapshot into a local image with an alias; subsequent launches from that alias produce instances with identical starting state.

**Clustering:** `incus cluster` joins multiple hosts into one logical Incus that schedules instances across member nodes and shares the image store, presented through the same CLI and API. No external control plane is introduced.

Incus does not replace application-container tooling. It applies where the unit of work is a machine rather than a packaged process: continuous-integration runners, per-project development boxes, and hosts where containers and virtual machines are administered through one command.

## Release lines and packaging

Incus publishes **a long-term-support (LTS) line alongside a frequent feature-release stream**; the LTS line receives fixes without the feature churn, which is the line distributions package. Incus is present in Debian (from Debian 13) and available on Ubuntu, Fedora and Arch. For builds newer than a distribution ships, the project points at the **Zabbly** repositories maintained by Stéphane Graber, which carry both the stable and LTS channels for Debian and Ubuntu.

Installation from Zabbly on a Debian or Ubuntu host:

```sh
curl -fsSL https://pkgs.zabbly.com/key.asc | sudo tee /etc/apt/keyrings/zabbly.asc
CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
sudo sh -c "cat > /etc/apt/sources.list.d/zabbly-incus-stable.sources" <<EOF
Enabled: yes
Types: deb
URIs: https://pkgs.zabbly.com/incus/stable
Suites: $CODENAME
Components: main
Signed-By: /etc/apt/keyrings/zabbly.asc
EOF
sudo apt update && sudo apt install -y incus
sudo incus admin init --minimal      # storage pool + default network, no prompts
```

## Migrating off LXD

Migration is not a dump-and-reimport. Incus ships **`lxd-to-incus`**, an official one-shot migrator that reads the running LXD daemon's instances, profiles, storage pools and networks and transfers them in place, leaving LXD stopped afterwards:

```sh
sudo lxd-to-incus          # interactive; verifies both daemons, then transfers state
```

**The migrator refuses to run on configurations it cannot translate cleanly**, so an untranslatable setup surfaces as a failure during preflight rather than as silently missing data after the fact. The preflight output is the record of what will move.

## Pitfalls

- Omitting `--` in `incus exec web1 ls -l` causes the Incus client to consume `-l` as its own flag; the guest command then runs without it, or the invocation errors on an unrecognised option.
- Editing a shared profile changes every instance referencing it, including production instances that were never the target of the change — a memory limit lowered in `default` applies fleet-wide.
- A quoted heredoc delimiter (`<<'EOF'`) suppresses shell expansion, so a `Suites:` line containing `$(. /etc/os-release && echo $VERSION_CODENAME)` is written literally into the `.sources` file and `apt update` fails to resolve the suite. Expand the codename before the heredoc, or leave the delimiter unquoted.
- `incus delete` on a running instance is rejected; the instance must reach `STOPPED` first, which is why teardown is `incus stop` followed by `incus delete`.
- A container shares the host kernel, so a guest requiring a kernel module absent on the host, or a different kernel version, will fail at runtime rather than at launch. That workload belongs in a `--vm` instance.
- Treating system containers as immutable images leads to drift: package updates, log growth and local configuration accumulate inside a long-lived instance, and only an explicit `incus snapshot create` captures a recoverable point.
- `incus publish` operates on a snapshot of an instance; publishing a running instance without first snapshotting captures whatever state the filesystem happens to be in.
