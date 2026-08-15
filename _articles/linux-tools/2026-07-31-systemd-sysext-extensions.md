---
title: "Layering Tools onto Immutable /usr with systemd-sysext"
date: 2026-07-31
track: linux-tools
summary: "How systemd-sysext overlays extra binaries and configuration onto a read-only /usr and /opt at runtime with overlayfs, with a build-and-merge walkthrough on systemd 261."
reading_time: 6
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

**Gist.** An immutable operating system ships `/usr` read-only, often on a verity-protected
partition, which forecloses the ordinary remedy of copying a missing diagnostic binary into place.
`systemd-sysext` resolves this by mounting an **overlayfs** whose lower layers are the host's `/usr`
and `/opt` and whose upper layers come from separately packaged *system extension* trees, leaving
the base image byte-for-byte unmodified. The cost is a compatibility contract — every extension must
carry an extension-release marker that matches the host, and an extension whose marker does not
match is skipped rather than merged.

## Scope of the merge

A system extension is a directory tree or a `.raw` disk image containing a `/usr` hierarchy, an
`/opt` hierarchy, or both. `systemd-sysext merge` enumerates the enabled extensions, validates each
against the host, and combines their hierarchies with the host's own through overlayfs. **Content
outside `/usr` and `/opt` is ignored entirely**, so an extension cannot reach into `/etc`, `/var` or
a user's home directory. `unmerge` dismounts the overlay; because the overlay is a mount and not a
copy, **nothing on the base image was ever written**, and the state does not survive a reboot unless
a merge is performed again.

That reversibility is the operative difference from copying a binary into place. A copy is a
persistent mutation that must be tracked and undone; a merge is a mount whose removal is a single
unmount. `systemd-confext` performs the same operation for `/etc`.

Both tools ship with systemd, current release **261** (June 2026).

## Search paths

sysext scans, in order:

- `/etc/extensions/`
- `/run/extensions/`
- `/var/lib/extensions/` — the conventional location for locally built trees
- `/usr/lib/extensions/`

confext scans the correspondingly named `confexts` directories under the same prefixes.

## The extension-release marker

The one non-optional component of an extension is the marker file at

```
/usr/lib/extension-release.d/extension-release.NAME
```

where `NAME` matches the extension's directory or image name. confext uses
`/etc/extension-release.d/extension-release.NAME`. The marker is the mechanism by which systemd
declines to overlay an extension built against a different operating system. The relevant fields:

- `ID=` — must equal the host's `ID` from `/etc/os-release`, or `_any` to bypass the check.
- `VERSION_ID=` — must match the host, and is checked **only when `SYSEXT_LEVEL=` is unset**.
- `SYSEXT_LEVEL=` — an independent compatibility version; when present it is checked **instead of**
  `VERSION_ID`.
- `ARCHITECTURE=` — must match the kernel architecture, or `_any`.
- `EXTENSION_RELOAD_MANAGER=1` — instructs systemd to reload the service manager after the merge,
  which is required for an extension that contributes unit files.

The failure mode is quiet: **an extension whose fields do not match is skipped, and the
merge of the remaining extensions still succeeds**. A missing tool after `merge` is therefore
diagnosed at `systemd-sysext list` and `status`, not from a non-zero exit code.

## Building and merging an extension

The following constructs an extension supplying `strace` on a host that omitted it.

```bash
# 1. Create the hierarchy. Binaries go under the extension's own /usr.
sudo mkdir -p /var/lib/extensions/mytools/usr/bin
sudo cp /path/to/strace /var/lib/extensions/mytools/usr/bin/

# 2. Write the compatibility marker, derived from THIS host.
. /etc/os-release
sudo mkdir -p /var/lib/extensions/mytools/usr/lib/extension-release.d
printf 'ID=%s\nVERSION_ID=%s\n' "$ID" "$VERSION_ID" | \
  sudo tee /var/lib/extensions/mytools/usr/lib/extension-release.d/extension-release.mytools

# 3. Confirm detection prior to activation.
systemd-sysext list

# 4. Merge: overlay the hierarchy onto /usr.
sudo systemd-sysext merge

# 5. Verify.
systemd-sysext status
strace -V
```

`status` reports the merge state of `/usr` and `/opt` and which extensions are currently layered.
The inverse operation is:

```bash
sudo systemd-sysext unmerge
```

Adding or removing an extension while the overlay is mounted requires `refresh`, which performs the
unmerge and merge as one step. Merging at boot is arranged by enabling `systemd-sysext.service`.

The overlay is **read-only by default**. `--mutable=` redirects writes into
`/var/lib/extensions.mutable/`. With `--mutable=ephemeral` the writes are discarded at unmerge;
with `--mutable=yes` they persist in that directory.

## Versioning with sysupdate

Extensions are intended to be shipped and versioned like the base image rather than edited in place
on the host. `systemd-sysupdate` is the companion for that path: `.raw` extension images are
published to an HTTP(S) or local source, described by a transfer definition, and sysupdate
downloads, verifies and atomically swaps in new versions — the same mechanism that updates the
operating system image itself.

`SYSEXT_LEVEL=` is what makes this workable across base-image churn. It is compared against the
host's own `SYSEXT_LEVEL=` in `/etc/os-release`, so the host image must declare one; where it does,
**an extension pinned to a `SYSEXT_LEVEL` remains compatible across host `VERSION_ID` changes** and
need not be rebuilt for every base-image update.

## confext

Where the material to overlay is configuration rather than executables — files destined for `/etc`
on an image whose `/etc` is otherwise managed — `systemd-confext merge` applies. It operates
identically but targets `/etc/`, and its marker lives in `/etc/extension-release.d/`.

## Pitfalls

- **`merge` reports success but the tool is still absent.** The extension's `ID=`, `VERSION_ID=` or
  `ARCHITECTURE=` did not match the host, so that extension alone was skipped; the exit status
  reflects the merge as a whole, not per-extension acceptance.
- **`VERSION_ID=` in the marker is ignored.** `SYSEXT_LEVEL=` is also set, and it is checked instead
  of `VERSION_ID=`, not in addition to it.
- **The extension breaks after a host upgrade.** A marker pinned to `VERSION_ID=` matches exactly one
  host version; a `SYSEXT_LEVEL=`-based marker is the field designed to be tracked independently.
- **Files placed outside `/usr` and `/opt` never appear.** sysext merges those two hierarchies only,
  and content elsewhere in the extension tree is ignored without a diagnostic.
- **New unit files are not visible to the service manager.** The marker omits
  `EXTENSION_RELOAD_MANAGER=1`, so no reload was performed after the merge.
- **Writes into the overlay fail.** The overlay is read-only unless `--mutable=` is passed; with
  `--mutable=ephemeral` the writes succeed and are then discarded at unmerge.
- **The extension disappears after a reboot.** A merge is a mount, not a modification of the base
  image; persistence across boots requires `systemd-sysext.service` to be enabled.
- **Adding an extension while merged has no effect.** The existing overlay mount is unchanged until
  `refresh` remounts it.
