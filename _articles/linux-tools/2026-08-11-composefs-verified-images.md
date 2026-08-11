---
title: "composefs: content-addressed, tamper-evident root filesystems"
date: 2026-08-11
track: linux-tools
summary: "composefs stacks EROFS, overlayfs, and fs-verity into a read-only mount whose files are content-addressed and shared across images, with the whole tree pinned to a single root digest. It's the integrity layer under ostree and bootc's immutable-OS story."
reading_time: 6
tags: [composefs, fs-verity, erofs, ostree, immutable, integrity]
sources:
  - title: "composefs/composefs — README"
    url: "https://github.com/composefs/composefs/blob/main/README.md"
  - title: "mount.composefs(1) manual page"
    url: "https://github.com/composefs/composefs/blob/main/man/mount.composefs.md"
  - title: "Composefs state of the union — Alexander Larsson"
    url: "https://blogs.gnome.org/alexl/2023/07/11/composefs-state-of-the-union/"
  - title: "Using composefs with OSTree — ostreedev docs"
    url: "https://ostreedev.github.io/ostree/composefs/"
  - title: "composefs backend — bootc documentation"
    url: "https://bootc.dev/bootc/experimental-composefs.html"
---

An immutable OS makes a promise: the running root filesystem is exactly the one that was built, byte for byte, and nothing has swapped a binary underneath you. dm-verity keeps that promise for a whole block device, but it does so by freezing an image — you lose the file-level sharing that makes container storage cheap. **composefs** is the project that squares that circle. Its tagline says it plainly: "the reliability of disk images, the flexibility of files."

## What it actually is

composefs is not a filesystem driver of its own. It stores no persistent data. Instead it composes three existing kernel features into a read-only mount:

- **EROFS** holds the *metadata* layer — the directory tree, inodes, permissions, and xattrs — as a compact, mmap-friendly image. Crucially, this EROFS image contains no file *contents*.
- **overlayfs** is the mount mechanism. Each metadata inode carries a `trusted.overlay.redirect` xattr pointing at where the real bytes live, and overlayfs resolves those redirects at read time.
- **fs-verity** (optional) provides the integrity chain, validating both the metadata image and each backing file against an expected digest.

The file bytes themselves live in a separate **content-addressed object store** — a directory of files named by their hash, exactly like ostree or a container layer store. Because two images that contain the same `/usr/bin/bash` redirect to the same object, that file exists once on disk and is cached once in the page cache, no matter how many mounted images reference it. You get disk and RAM deduplication *across* images while each image keeps its own independent metadata.

## Why the digest matters

Here is the property that makes composefs interesting for security, not just storage. Every backing object can have fs-verity enabled, so the kernel computes and enforces a Merkle-tree digest for its contents on every read. The metadata image records the expected per-file digests, so a swapped object is rejected. And the metadata image *itself* has a single fs-verity digest — a root digest that covers the entire directory structure and, transitively, every file it points at.

That means you can reduce "is this root filesystem the one I built?" to a single hash comparison. Pin the root digest — in an initrd, in a signed commit, in firmware — and any tampering anywhere in the tree changes it. This is the integrity guarantee of dm-verity with the file-level sharing of a content store.

## Building and mounting one

The tooling ships in the `composefs` package (the C library is `libcomposefs`); as of 2026 the project sits at the stable 1.0.x series (v1.0.8, January 2025), with the userspace helpers below. Start from an ordinary directory tree:

```sh
# Build a metadata image and populate a content-addressed object store.
# --digest-store writes each file's bytes into ./objects, hashed and
# (where the kernel supports it) with fs-verity enabled automatically.
mkcomposefs --digest-store=objects /path/to/rootfs image.cfs

# Print the root digest that covers the whole tree.
mkcomposefs --print-digest-only /path/to/rootfs
# -> sha256:9a5f...c17

# Mount it, resolving file contents out of ./objects.
sudo mount -t composefs -o basedir=objects image.cfs /mnt
```

The mount above trusts the local files. To make it *verified*, hand `mount.composefs` the expected root digest and let it refuse to mount anything else:

```sh
sudo mount.composefs -o basedir=objects,digest=9a5f...c17 image.cfs /mnt
```

The `digest=` option validates `image.cfs` against that fs-verity digest before use and automatically turns on verity checking, establishing a trust chain from the root digest down to individual files. Going further, `-o verity` requires that *every* file in the image carry an fs-verity digest and that each backing object match it — that mode needs a kernel with file-backed EROFS verity support (6.6 or newer).

| Layer | Role | Verified by |
|-------|------|-------------|
| EROFS image | directory tree + metadata | fs-verity digest of `image.cfs` |
| overlayfs | mount / redirect resolution | — (mechanism) |
| Object store | file contents, deduplicated | per-file fs-verity digests |

## Where it's being adopted

composefs was designed with ostree in mind, and that is where it has landed first. ostree can generate composefs metadata for a commit (`--generate-composefs-metadata`), storing the digest under `ostree.composefs.v0` so it is covered by the commit's signature. At boot, `ostree prepare-root` reads `ostree/prepare-root.conf` and, depending on the `composefs` setting — `enabled = yes`, `signed`, or `verity` — mounts `/` as a composefs image and can require an Ed25519 signature over the expected digest. A common pattern uses a transient per-build keypair: sign the digest, embed the public key in the initrd, discard the private key. The result is a fully verified, read-only `/usr`.

**bootc**, covered previously in [bootc: ship your Linux host as a container image](/articles/linux-tools/2026-07-31-bootc-bootable-containers), builds directly on this: bootc deploys OCI images via ostree, so composefs is the mechanism that makes a bootc host's root filesystem tamper-evident. bootc also has an experimental composefs-native backend that leans on it more directly. Downstream, this is the trajectory for the Fedora/CentOS Atomic and CoreOS family — the same content-addressed model that already backs their container images now backing the OS root itself. On the container side, `containers/storage` can use `mkcomposefs` to mount image layers the same way.

The through-line is that composefs is not a competitor to EROFS or fs-verity — it is the glue that turns them into a practical, deduplicating, end-to-end-verifiable root filesystem. EROFS gives it a compact metadata image, fs-verity gives it a root digest, overlayfs gives it a mount, and the content store gives it sharing.

**Try next:** build a composefs image from `/usr` on a test box with `mkcomposefs --digest-store=objects /usr usr.cfs`, note the `--print-digest-only` value, then mount it with `digest=` and confirm that flipping a single byte in an object file makes the read fail.
