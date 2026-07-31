---
title: "bootc: ship your Linux host as a container image, roll it back like one"
date: 2026-07-31
track: linux-tools
summary: "bootc lets you build a whole bootable OS as an OCI image, push it to a registry, and switch a running machine to it atomically — with a one-command rollback to the previous image. It's the same Containerfile you already know, applied to the host instead of an app."
reading_time: 5
tags: [bootc, ostree, containers, immutable, atomic-updates]
sources:
  - title: "bootc — Relationship with other projects (OSTree, rpm-ostree, podman)"
    url: "https://bootc.dev/bootc/relationships.html"
  - title: "Fedora — OstreeNativeContainerStable change"
    url: "https://fedoraproject.org/wiki/Changes/OstreeNativeContainerStable"
  - title: "rpm-ostree: ostree native containers"
    url: "https://coreos.github.io/rpm-ostree/container/"
---

Configuration management exists because a running Linux box drifts: packages get layered on by hand, `/etc` accumulates edits, and two machines that started identical slowly diverge. **bootc** attacks that from the other end. Instead of converging a mutable host toward a desired state, you *build* the desired state as an OCI container image and boot the machine from it. Updates are `podman pull` for the whole operating system, and rollback is picking the previous image at the bootloader.

## The model in one breath

bootc reuses the container toolchain as the *build and transport* mechanism, and OSTree underneath as the *on-disk deployment* mechanism. You write a `Containerfile` that `FROM`s a bootc base image and adds whatever you want baked in. The result isn't run by a container runtime — bootc pulls it (via skopeo), checks it out as an OSTree deployment, and wires it into the bootloader. Because the running system *is* the image, there's a clean definition of "correct", and updates are transactional: they stage into a new deployment and take effect on reboot, leaving the old one intact to roll back to.

```dockerfile
# Containerfile — this is your whole OS, as an image
FROM quay.io/fedora/fedora-bootc:42

RUN dnf -y install tmux vim htop chrony && dnf clean all
COPY chrony.conf /etc/chrony.conf
COPY sshd_hardening.conf /etc/ssh/sshd_config.d/10-hardening.conf
```

Build and push it like any other image:

```bash
podman build -t registry.example.com/fleet/edge-node:1.4 .
podman push registry.example.com/fleet/edge-node:1.4
```

## Switching a machine onto it

On a host already running a bootc image, point it at yours and reboot:

```bash
sudo bootc switch registry.example.com/fleet/edge-node:1.4
sudo systemctl reboot
```

From then on, `bootc upgrade` fetches whatever the tag now points to, stages it, and (with `--apply`) reboots into it. The commands that matter day to day:

```bash
bootc status      # what image am I booted into, and what's staged?
bootc upgrade      # pull + stage the newest image for this tag
bootc rollback     # make the PREVIOUS deployment the default again
```

`bootc rollback` is the part that changes how the 3 a.m. call goes. A bad update didn't mutate your host in place — the previous deployment is still on disk — so recovery is "boot the last image" rather than "figure out what the update touched". Many setups pair it with a health check that auto-rolls-back if the new boot doesn't come up clean.

## How it relates to what you know

If you've used **rpm-ostree** or Fedora Silverblue, bootc shares the same OSTree backing store; when the source is a container image, `rpm-ostree upgrade` and `bootc upgrade` are effectively equivalent. The difference is philosophy: bootc enforces a *pure* image model and errors if you try to mutate system state client-side, where rpm-ostree still permits package layering. That strictness is the point — it's what makes every machine on a tag bit-for-bit reproducible. This is the same mechanism behind RHEL "image mode" and Fedora/CentOS bootc images, so it's not a science project; it's shipping.

One honest limit: everything you want on the host goes in the image, so per-machine state (databases, `/var`, secrets) lives outside it and you plan for that explicitly. bootc gives you an immutable, reproducible *OS*; your stateful data is still your problem to manage.

**Try next:** in a throwaway VM, `bootc switch` to a base image, then build a derived image that just adds `htop`, push it, `bootc upgrade`, reboot, and confirm `htop` is present. Then run `bootc rollback` and watch it vanish on the next boot — that round trip is the entire value proposition in five minutes.
