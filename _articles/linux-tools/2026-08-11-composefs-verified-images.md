---
title: "composefs: content-addressed, tamper-evident root filesystems"
date: 2026-08-11
track: linux-tools
summary: "composefs stacks EROFS, overlayfs, and fs-verity into a read-only mount whose files are content-addressed and shared across images, with the whole tree pinned to a single root digest. It is the integrity layer under ostree and bootc's immutable-OS story."
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

**Gist.** An immutable operating system asserts that the running root filesystem is byte-for-byte the one that was built; device-mapper verity (dm-verity) enforces that assertion over a whole block device, but only by freezing an image, which forfeits the file-level sharing that makes container storage cheap. composefs obtains the same assertion at file granularity by splitting a tree into an Enhanced Read-Only File System (EROFS) metadata image whose inodes redirect, through overlayfs, into a content-addressed object store, with fs-verity digests chaining every object back to **a single root digest**. The cost is a three-layer mount whose integrity holds only when each link of that chain is enabled — an unverified mount is structurally identical to a verified one and fails open.

## Composition, not a new filesystem

composefs implements no on-disk format of its own and stores no persistent data. It composes three existing kernel features into one read-only mount:

- **EROFS** holds the *metadata* layer — directory tree, inodes, permissions, extended attributes (xattrs) — as a compact, mmap-friendly image. This image contains **no file contents**.
- **overlayfs** is the mount mechanism. Each metadata inode carries a `trusted.overlay.redirect` xattr naming where the real bytes live, and overlayfs resolves those redirects at read time.
- **fs-verity** supplies the integrity chain, validating both the metadata image and each backing file against an expected digest. It is optional, and that optionality is the security-relevant part.

File bytes live in a separate **content-addressed object store**: a directory of files named by their hash, on the same model as ostree or a container layer store. Two images containing the same `/usr/bin/bash` redirect to the same object, so that file occupies one copy on disk and **one set of page-cache pages**, irrespective of how many mounted images reference it. Deduplication is therefore *across* images, while each image retains independent metadata — a rename or permission change in one image rewrites only its EROFS blob.

## The digest chain

fs-verity computes a Merkle tree over a file's contents and has the kernel enforce it on every read, so corruption is detected at page-fault time rather than at open time. composefs uses this at two levels. Each backing object may have fs-verity enabled, and the metadata image records the expected per-file digest, so **a substituted object is rejected at read**. The metadata image in turn has its own fs-verity digest, a **root digest covering the entire directory structure and, transitively, every file the structure points at**.

The consequence is that the question "is this root filesystem the one that was built?" reduces to one hash comparison. Pinning the root digest — in an initramfs, in a signed commit, in firmware — makes any modification anywhere in the tree observable as a changed digest. The guarantee is dm-verity's; the storage layout is a content store's.

| Layer | Role | Verified by |
|-------|------|-------------|
| EROFS image | directory tree + metadata | fs-verity digest of `image.cfs` |
| overlayfs | mount / redirect resolution | — (mechanism) |
| Object store | file contents, deduplicated | per-file fs-verity digests |

## Building and mounting

The tooling ships in the `composefs` package; the C library is `libcomposefs`. Construction begins from an ordinary directory tree:

```sh
# Build a metadata image and populate a content-addressed object store.
# --digest-store writes each file's bytes into ./objects, hashed and
# (where the kernel supports it) with fs-verity enabled automatically.
mkcomposefs --digest-store=objects /path/to/rootfs image.cfs

# Print the root digest that covers the whole tree.
mkcomposefs --print-digest-only /path/to/rootfs
# -> 9a5f...c17

# Mount it, resolving file contents out of ./objects.
sudo mount -t composefs -o basedir=objects image.cfs /mnt
```

That mount trusts whatever is on local disk: it reads `image.cfs` as given and follows its redirects. Verification requires handing `mount.composefs` the expected root digest, which makes the mount itself the enforcement point:

```sh
sudo mount.composefs -o basedir=objects,digest=9a5f...c17 image.cfs /mnt
```

`digest=` validates `image.cfs` against that fs-verity digest **before use** and turns on verity checking, establishing the chain from root digest down to individual files; a mismatched image fails the mount rather than producing a subtly wrong tree. The stricter `-o verity` requires that *every* file in the image carry an fs-verity digest and that each backing object match it. That mode depends on the running kernel supporting verity checking through overlayfs; where that support is absent the mount fails rather than falling back to an unverified tree.

## Adoption

composefs was designed with ostree in mind, and ostree is where it landed first. ostree can generate composefs metadata for a commit (`--generate-composefs-metadata`), storing the digest as commit metadata so that it falls inside the region covered by the commit's signature. At boot, `ostree prepare-root` reads `ostree/prepare-root.conf`; its `[composefs]` section decides whether `/` is mounted as a composefs image and whether the expected digest must carry a valid signature. One documented pattern uses a transient per-build keypair: sign the digest, embed the public key in the initramfs, discard the private key. The result is a verified, read-only `/usr`.

**bootc**, covered previously in [bootc: shipping a Linux host as a container image](/articles/linux-tools/2026-07-31-bootc-bootable-containers), builds on this directly: bootc deploys Open Container Initiative (OCI) images by way of ostree, so composefs is the mechanism that renders a bootc host's root filesystem tamper-evident. bootc additionally carries an experimental composefs-native backend. Downstream this is the trajectory for the Fedora/CentOS Atomic and CoreOS family, where the content-addressed model already backing container images comes to back the operating-system root as well. On the container side, `containers/storage` can invoke `mkcomposefs` to mount image layers the same way.

composefs is not an alternative to EROFS or fs-verity but the glue that turns them into a deduplicating, end-to-end-verifiable root filesystem: EROFS supplies the compact metadata image, fs-verity the root digest, overlayfs the mount, and the object store the sharing.

## Pitfalls

- **Mounting without `digest=` verifies nothing.** The mount succeeds and the tree looks correct, because `basedir=` alone instructs overlayfs to follow redirects into whatever objects exist locally; the failure is silent by construction.
- **`--digest-store` enables fs-verity only where the kernel supports it.** On a filesystem or kernel lacking fs-verity, objects are written without digests, and an image built there will not satisfy `-o verity` later.
- **`-o verity` fails on a kernel without verity support in overlayfs**, not because any digest is wrong; the diagnostic points at the mount, not at the image.
- **A modified file changes the root digest.** Any pinned digest held in an initramfs, a signed commit, or firmware becomes stale after a rebuild, and the mount refuses rather than degrading.
- **Objects are shared across images.** Deleting an object because one image was removed breaks every other image whose metadata redirects to it; reads fail at the redirect, not at mount time.
- **Metadata lives in EROFS, contents do not.** Copying `image.cfs` to another host without the corresponding object store yields a mountable tree whose files are unreadable.
