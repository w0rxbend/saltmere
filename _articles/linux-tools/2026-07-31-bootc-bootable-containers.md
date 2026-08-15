---
title: "bootc: shipping a Linux host as a container image"
date: 2026-07-31
track: linux-tools
summary: "bootc builds an entire bootable operating system as an OCI image, transports it through a registry, and switches a running machine onto it transactionally, with rollback to the previous deployment. The build input is an ordinary Containerfile; the target is the host rather than an application."
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

**Gist.** A running Linux host drifts: packages are layered by hand, `/etc` accumulates edits, and two machines that began identical diverge without any record of how. bootc removes the convergence problem by making the operating system a build artefact — an Open Container Initiative (OCI) image — that is pulled from a registry and checked out as an OSTree deployment, so an update is a whole-image replacement staged beside the running system and rollback is the selection of the previous deployment. The cost is that the image is the only supported channel for host state: anything not baked into it must live in mutable directories that the image model deliberately does not own, and every change to the host requires a rebuild, a push, and a reboot.

## Two mechanisms, cleanly separated

bootc does not invent a packaging format. It uses the **container toolchain as build and transport** and **OSTree as the on-disk deployment mechanism**. Those roles do not overlap, and keeping them apart is what makes the model legible.

The build side is an ordinary `Containerfile` whose base is a bootc base image. Every layer, cache and registry behaviour that applies to application images applies here unchanged, because the artefact genuinely is an OCI image.

```dockerfile
# Containerfile — the entire operating system, expressed as an image
FROM quay.io/fedora/fedora-bootc:42

RUN dnf -y install tmux vim htop chrony && dnf clean all
COPY chrony.conf /etc/chrony.conf
COPY sshd_hardening.conf /etc/ssh/sshd_config.d/10-hardening.conf
```

```bash
podman build -t registry.example.com/fleet/edge-node:1.4 .
podman push registry.example.com/fleet/edge-node:1.4
```

The deployment side is where the container runtime stops being involved. **The image is never executed by a container runtime on the target host.** bootc fetches it over the ordinary registry protocol, checks its contents out as an OSTree deployment, and wires that deployment into the bootloader. The running system is a checkout of the image rather than a process isolated from the host — the isolation primitives that define a container at runtime (namespaces, cgroups) play no part.

## The state machine of an update

The property that matters operationally is that **an update never mutates the deployment that is currently booted**. The sequence has three distinguishable states:

1. **Booted.** One deployment is live. Its content corresponds to a specific image digest, reported by `bootc status`.
2. **Staged.** `bootc upgrade` resolves the tag the host is tracking, pulls the image if the digest changed, and materialises a *new* deployment on disk. The booted deployment is untouched. A crash, a power loss or an operator's change of mind at this point leaves the machine exactly as it was.
3. **Applied.** A reboot makes the staged deployment the default. The previous deployment remains on disk as the rollback target.

```bash
bootc status      # booted image and digest; whether a deployment is staged
bootc upgrade     # resolve the tag, pull, stage a new deployment
bootc rollback    # make the PREVIOUS deployment the default again
```

Moving a host onto a different image — a different repository or tag rather than a newer build of the same tag — is `bootc switch`:

```bash
sudo bootc switch registry.example.com/fleet/edge-node:1.4
sudo systemctl reboot
```

`bootc upgrade --apply` collapses the stage-and-reboot pair into one command.

The consequence for recovery is structural rather than procedural. Because a bad update did not modify anything in the previous deployment, recovery does not require determining what the update touched; the previous deployment is still present and bootable. This is what makes an automated health check paired with `bootc rollback` a coherent design: the rollback target is known before the upgrade begins, not reconstructed after the failure.

## Relationship to rpm-ostree

bootc and rpm-ostree share the same OSTree backing store. When the source of a deployment is a container image, `rpm-ostree upgrade` and `bootc upgrade` are effectively equivalent operations; Fedora's OstreeNativeContainerStable change made the container-native path the stable delivery mechanism.

The difference is in what each tool permits after boot. **rpm-ostree allows client-side package layering — modifications applied on the machine, on top of the image.** bootc offers no client-side layering operation: the supported way to add a package is a derived image. The practical effect is on the identity of a fleet: with layering permitted, a tag identifies a *base* that hosts may have extended differently; with layering refused, the digest a host reports in `bootc status` fully determines the operating system content. The same mechanism underlies Red Hat Enterprise Linux (RHEL) "image mode" and the Fedora and CentOS bootc images.

## What the image does not cover

The model applies to the operating system, not to the machine's data. Per-machine state — databases, the contents of `/var`, secrets — is outside the image by construction, and must be planned for as a separate concern with its own backup and migration story. An image rollback returns the operating system to a previous state; it does not return data written by the newer software to a schema the older software understands.

A second consequence of the pure image model is latency of change. **Every host modification, including a one-line configuration edit, is a rebuild, a registry push, an upgrade and a reboot.** There is no supported in-place edit that survives as part of the defined system state.

A minimal end-to-end exercise in a disposable virtual machine: `bootc switch` to a base image, build a derived image adding a single package, push it, run `bootc upgrade`, reboot, confirm the package is present, then `bootc rollback` and confirm it is absent after the next boot. That round trip exercises transport, staging, activation and reversal.

## Pitfalls

- **Expecting a configuration edit to persist as system state.** A file changed by hand on a booted deployment is not part of the image; the next upgrade replaces the deployment with a checkout of the new image, and any change not expressed in the `Containerfile` is absent from it.
- **Treating rollback as a data rollback.** `bootc rollback` restores the previous operating system deployment. Data written to `/var` by the newer release remains as the newer release left it, including schema migrations the older release cannot read.
- **Tracking a mutable tag across a fleet.** `bootc upgrade` resolves whatever the tag currently points to, so two hosts upgrading at different times can land on different digests from the same tag. `bootc status` reports the digest; the tag alone does not identify the running system.
- **Assuming a container runtime is involved at boot.** The image is checked out as an OSTree deployment, not run. Debugging steps that depend on `podman` inspecting a running container do not apply to the host itself.
- **Looking for a client-side package-layering command under bootc.** bootc exposes none; the equivalent operation is a new derived image and an upgrade.
- **Ignoring registry availability in the update path.** An upgrade requires reaching the registry to resolve the tag and fetch the image; a host that cannot reach it stays on its current deployment rather than partially updating.
