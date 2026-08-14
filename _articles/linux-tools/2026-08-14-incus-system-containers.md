---
title: "Incus: System Containers and VMs After the LXD Fork"
date: 2026-08-14
track: linux-tools
summary: "Incus is the community fork of LXD, now under Linux Containers, that runs full-system LXC containers and QEMU virtual machines behind one CLI and one API — here is where it stands in 2026, how to migrate off LXD, and the commands to launch both."
reading_time: 5
tags: [incus, lxd, lxc, containers, virtual-machines, linux-containers]
sources:
  - title: "Incus documentation — Introduction"
    url: "https://linuxcontainers.org/incus/docs/main/"
  - title: "Introducing Incus — Linux Containers announcement"
    url: "https://linuxcontainers.org/incus/announcement/"
  - title: "Migrating from LXD — Incus documentation"
    url: "https://linuxcontainers.org/incus/docs/main/howto/server_migrate_lxd/"
  - title: "Zabbly — Incus package repository"
    url: "https://zabbly.com/incus/"
---

If you ran LXD, you already know its trick: it manages *system* containers — full Linux userlands with their own init, logs, and package manager, closer to a lightweight VM than to a Docker process. In **August 2023** Canonical moved LXD out of the Linux Containers project and under its own umbrella. Days later, Aleksa Sarai forked the last community LXD into **Incus**, and the Linux Containers team — including original LXD author Stéphane Graber — adopted it as the community-led continuation. Two and a half years on, Incus is the default in Debian and a widely packaged, actively released project.

## System containers, not application containers

Incus is deliberately not a Docker replacement. The split is about *what runs as PID 1*:

- An **application (OCI) container** runs a single process — your service — and dies when it exits. That's Docker, Podman, containerd.
- A **system container** boots a full OS: systemd (or another init), cron, sshd, whatever you install. You `exec` into it and it behaves like a small machine, but it shares the host kernel, so it starts in under a second with near-zero overhead.

The pitch is that one tool covers the whole range. Incus drives **LXC** for system containers and **QEMU** for virtual machines through a single CLI and REST API — so a container and a full VM are launched, listed, and managed with the same verbs, the VM differing only by a `--vm` flag. When you need real kernel isolation (different kernel, nested virt, untrusted workloads) you reach for the VM; when you don't, the container is cheaper.

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

## Launching a container and a VM

Same command, one flag apart:

```sh
# a system container from the images: remote
incus launch images:debian/13 web

# a full virtual machine — note --vm
incus launch images:debian/13 buildvm --vm

incus list
# +--------+---------+---------------------+------+-----------------+-----------+
# | NAME   | STATE   | IPV4                | IPV6 | TYPE            | SNAPSHOTS |
# +--------+---------+---------------------+------+-----------------+-----------+
# | web    | RUNNING | 10.x.x.20 (eth0)    |      | CONTAINER       | 0         |
# | buildvm| RUNNING | 10.x.x.21 (enp5s0)  |      | VIRTUAL-MACHINE | 0         |
# +--------+---------+---------------------+------+-----------------+-----------+

incus exec web -- bash                 # drop into a shell inside the container
incus exec buildvm -- systemctl status # same verb reaches into the VM
```

## Migrating off LXD

You don't dump and re-import. Incus ships **`lxd-to-incus`**, an official one-shot migrator that reads your running LXD's instances, profiles, storage pools, and networks and moves them over in place, leaving LXD stopped afterward:

```sh
sudo lxd-to-incus          # interactive; verifies both daemons, then transfers state
```

It refuses to run on configurations it can't translate cleanly, so it fails loudly rather than silently dropping data — read its preflight output before confirming.

**Try next:** On a spare Debian/Ubuntu box, install Incus from Zabbly, `incus launch images:debian/13 t1` and `incus launch images:debian/13 t2 --vm`, then `incus list` and compare how fast each reaches `RUNNING`.
