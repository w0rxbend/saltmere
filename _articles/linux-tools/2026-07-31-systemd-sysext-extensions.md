---
title: "Layering Tools onto Immutable /usr with systemd-sysext"
date: 2026-07-31
track: linux-tools
summary: "How systemd-sysext overlays extra binaries and config onto a read-only /usr and /opt at runtime with overlayfs, plus a build-and-merge walkthrough on systemd 261."
reading_time: 5
tags: [systemd, sysext, immutable, overlayfs, linux]
sources:
  - title: "systemd-sysext(8) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man8/systemd-sysext.8.html"
  - title: "systemd 261 Released With New systemd-sysinstall OS Installer — Phoronix"
    url: "https://www.phoronix.com/news/systemd-261"
  - title: "The systemd 261 release brings a software TPM, new OS installer — Help Net Security"
    url: "https://www.helpnetsecurity.com/2026/06/22/systemd-261-released/"
  - title: "Keep Your Base OS Clean: Practical systemd-sysext — DEV Community"
    url: "https://dev.to/lyraalishaikh/keep-your-base-os-clean-practical-systemd-sysext-for-linux-tools-and-overrides-395n"
---

On an immutable OS the whole point of `/usr` is that you cannot write to it. Fedora Silverblue, bootc-based images, embedded appliances, and stripped-down container base images all ship a `/usr` that is read-only (often on a verity-protected partition). That is great for integrity and atomic updates, and painful the moment you need `strace` on a box that shipped without it. `systemd-sysext` is the sanctioned escape hatch: it layers extra hierarchies onto `/usr` and `/opt` at runtime with overlayfs, no rebuild of the base image required. Its sibling `systemd-confext` does the same trick for `/etc`. Both ship with systemd, current release **261** (June 19, 2026).

## What actually happens on merge

A *system extension* is just a directory tree (or a `.raw` disk image) that contains a `/usr` and/or `/opt` hierarchy. When you run `systemd-sysext merge`, systemd finds every enabled extension, checks each one is compatible with the host, and combines their `/usr` and `/opt` with the host's own via overlayfs. Files anywhere outside `/usr` and `/opt` are ignored entirely. `unmerge` tears the overlay back down; nothing on the base image was ever modified.

Because it is an overlay, the base stays read-only and the merge is fully reversible across a reboot. That is the key difference from just `cp`-ing a binary somewhere: there is no persistent mutation of the host to undo.

## Where extensions live and the compatibility marker

sysext scans these directories, in order:

- `/etc/extensions/`
- `/run/extensions/`
- `/var/lib/extensions/` — the usual place you drop things
- `/usr/lib/extensions/`

confext scans the `confexts` equivalents (`/run/confexts/`, `/var/lib/confexts/`, `/usr/lib/confexts/`).

The one non-optional piece is the **extension-release marker**. Every sysext must contain a file at:

```
/usr/lib/extension-release.d/extension-release.NAME
```

where `NAME` matches the extension's directory or image name. (confext uses `/etc/extension-release.d/extension-release.NAME`.) This file is how systemd refuses to merge an extension built for the wrong OS. Relevant fields:

- `ID=` — must equal the host's `ID` from `/etc/os-release`, or `_any` to skip the check.
- `VERSION_ID=` — must match the host when `SYSEXT_LEVEL=` is not set.
- `SYSEXT_LEVEL=` — an independent compatibility version; if set, it is checked instead of `VERSION_ID`.
- `ARCHITECTURE=` — must match the kernel arch, or `_any`.
- `EXTENSION_RELOAD_MANAGER=1` — tells systemd to reload the service manager after merge (use it if your extension adds unit files).

If none of these match, `merge` skips the extension rather than silently overlaying an incompatible one.

## Step-by-step: build and merge an extension

Say you want `strace` (or any tool) available on a host that omitted it. Build a tree under `/var/lib/extensions`.

```bash
# 1. Create the hierarchy. Binaries go under the extension's /usr.
sudo mkdir -p /var/lib/extensions/mytools/usr/bin
sudo cp /path/to/strace /var/lib/extensions/mytools/usr/bin/

# 2. Add the compatibility marker matching THIS host.
. /etc/os-release
sudo mkdir -p /var/lib/extensions/mytools/usr/lib/extension-release.d
printf 'ID=%s\nVERSION_ID=%s\n' "$ID" "$VERSION_ID" | \
  sudo tee /var/lib/extensions/mytools/usr/lib/extension-release.d/extension-release.mytools

# 3. See it detected but not yet active.
systemd-sysext list

# 4. Merge — overlays it onto /usr.
sudo systemd-sysext merge

# 5. Confirm and use.
systemd-sysext status
strace -V
```

`status` shows the merge state of `/usr` and `/opt` and which extensions are layered. To undo:

```bash
sudo systemd-sysext unmerge
```

If you add or remove extensions while merged, `refresh` remounts the overlay in one step (unmerge + merge). To make merge-at-boot automatic, enable `systemd-sysext.service`.

One caveat worth knowing: the overlay is read-only by default. If you need writes to land somewhere, `--mutable=` routes them into `/var/lib/extensions.mutable/`, with modes ranging from `ephemeral` (changes discarded on unmerge) to `enabled` (persistent).

## Keeping extensions updated: sysupdate

Extensions are meant to be shipped and versioned like the base image, not hand-edited on the host. `systemd-sysupdate` is the companion for that: you publish `.raw` extension images to an HTTP(S) or local source, describe them with a transfer definition, and `sysupdate` downloads, verifies, and atomically swaps in new versions — the same mechanism used to update the OS image itself. The `SYSEXT_LEVEL` field is what lets an extension track its own compatibility version independently of the host `VERSION_ID`, so an extension can survive several base-image updates without rebuilding.

## When to reach for confext instead

If what you need to overlay is configuration rather than binaries — dropping files into `/etc` on an image where `/etc` is otherwise managed — use `systemd-confext merge`. It works identically but targets `/etc/` and mounts with `nosuid`/`noexec` by default, since config directories should never carry executables.

**Try next:** On any systemd 261 host, build the `mytools` extension above but point it at a trivial script in `usr/bin/hello`, merge it, then run `systemd-sysext unmerge` and confirm the file vanishes from `/usr/bin` — proof the base `/usr` was never touched.
