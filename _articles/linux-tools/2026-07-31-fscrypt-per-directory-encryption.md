---
title: "fscrypt: encryption at directory granularity"
date: 2026-07-31
track: linux-tools
summary: "LUKS encrypts a block device — one key, all or nothing, unlocked at boot. fscrypt lives inside ext4, f2fs and UBIFS and applies a policy to a single directory tree, whose key is present in the kernel only while it is explicitly unlocked."
reading_time: 6
tags: [fscrypt, encryption, ext4, f2fs, security]
sources:
  - title: "Filesystem-level encryption (fscrypt) — Linux Kernel documentation"
    url: "https://www.kernel.org/doc/html/latest/filesystems/fscrypt.html"
  - title: "google/fscrypt — high-level management tool (policies, protectors, PAM)"
    url: "https://github.com/google/fscrypt"
  - title: "fscrypt — ArchWiki"
    url: "https://wiki.archlinux.org/title/Fscrypt"
---

**Gist.** Full-disk encryption with the Linux Unified Key Setup (LUKS) protects a block device with a single key that is supplied once at boot, after which every file on the device is plaintext to any process with the right permissions. **fscrypt** is encryption implemented inside the filesystem — ext4, f2fs and UBIFS — attaching an *encryption policy* to one directory tree so that its contents and filenames are readable only while that tree's master key is loaded in the kernel. The cost is that protection stops at file data and names: **sizes, timestamps, ownership, permissions and the shape of the directory hierarchy remain in cleartext metadata**, and once the key is loaded the plaintext is as reachable as any ordinary file.

## The policy and its inheritance

An encryption policy is stored in the inode of a directory. It records the cipher pair, a filename-padding parameter, a policy version, and an identifier for the master key — not the key itself. Setting a policy requires the directory to be **empty**; the interface offers no operation that encrypts files already present, so there is no in-place conversion path. Every file and subdirectory created below an encrypted directory **inherits that policy**, so the tree has one policy from its root downward and encryption cannot be selectively disabled inside it.

Two policy versions exist. **Version 1** identifies the master key by an 8-byte *master key descriptor* and looks the key up in process-subscribed keyrings, which means the key's visibility depends on which keyring the requesting process happens to be subscribed to. **Version 2** identifies the key by a 16-byte *key identifier* that is derived cryptographically from the master key itself, and the key is added to a keyring belonging to the filesystem rather than to a process. Version 2 also derives all subkeys with **HKDF-SHA512** (HMAC-based key derivation function, as specified in RFC 5869), with distinct context values separating the per-file content key from the key used for filenames. New deployments use v2; the `fscrypt` userspace tool defaults to it on kernels that support it.

## Contents, names, and what leaks

By default file contents are encrypted with **AES-256-XTS** and filenames with **AES-256-CTS-CBC**. Devices without AES acceleration can select **Adiantum**, a length-preserving construction intended for that case. Contents are encrypted per filesystem block with an initialisation vector derived from the block index, so a file's ciphertext is position-dependent; because keys are derived per file, **two files with identical contents under the same policy encrypt to different ciphertext**.

Filenames are padded before encryption to a multiple of a configured value — **4, 8, 16 or 32 bytes** — which bounds how much a filename's length leaks to the size class it falls into, at the cost of directory-entry space. Nothing pads file *contents*: the length of a file is visible from its inode regardless of the policy.

The metadata that stays readable is the load-bearing limitation. **fscrypt does not conceal file sizes, access and modification times, ownership, mode bits, extended attributes, or the number and nesting of directories.** It also assumes an intact kernel: the key lives in kernel memory while unlocked, so an attacker who can read kernel memory or run code in the kernel is not stopped by any policy.

## The lock/unlock state machine

Key management at the kernel level is two ioctls. `FS_IOC_ADD_ENCRYPTION_KEY` installs a master key into the filesystem's keyring, after which processes can open files under any policy naming that key. `FS_IOC_REMOVE_ENCRYPTION_KEY` removes it and then attempts to evict the cached inodes and page-cache pages that hold derived keys and plaintext.

That eviction is where the interesting failure lives. **If any file under the policy is still open, or any directory under it is a process's working directory, the removal cannot evict those inodes; the ioctl still removes the key but reports the flag `FSCRYPT_KEY_REMOVAL_STATUS_FLAG_FILES_BUSY`, and the in-use files remain readable until their last reference is dropped.** A lock operation that returns success is therefore not by itself proof that no plaintext is reachable.

Removal is also per-user for v2 policies. A key added by one user is *claimed* by that user; a non-root removal drops only that user's claim, and the key stays installed while another user's claim remains. Removing every claim at once is `FS_IOC_REMOVE_ENCRYPTION_KEY_ALL_USERS`, which requires root.

With the key absent, the directory is still listable, but each entry appears as a **base64-encoded ciphertext name**, and names too long to represent that way are replaced by a digest-bearing form. Opening such a file fails with **`ENOKEY`**. Both no-key names can be used for `rename` and `unlink`, so files can be moved or deleted without the key even though they cannot be read.

## Operating it

The filesystem needs the `encrypt` feature flag, set at creation time or afterwards on an unmounted ext4 volume:

```bash
sudo tune2fs -O encrypt /dev/sdaN          # unmounted; or mkfs.ext4 -O encrypt
```

The `fscrypt` tool from Google wraps the ioctls with *protectors* — a passphrase, a login password, or a raw key file — that wrap the master key, so the master key is never handled directly:

```bash
sudo fscrypt setup                          # write /etc/fscrypt.conf, prepare metadata
sudo fscrypt setup /mnt/data                # prepare this mountpoint

mkdir /mnt/data/secrets                     # must be empty when the policy is set
fscrypt encrypt /mnt/data/secrets
```

The steady-state cycle:

```bash
fscrypt status /mnt/data/secrets            # policy version, protectors, locked or not
fscrypt lock   /mnt/data/secrets            # remove the key, evict what it can
fscrypt unlock /mnt/data/secrets            # re-derive the master key from a protector
```

`fscryptctl` is a smaller C utility that issues the raw ioctls and performs no key management: the caller supplies the master key bytes and tracks them.

## Composition with LUKS

The property fscrypt expresses and a single LUKS volume cannot is **a distinct key per user on a shared machine**: with the tool's pluggable authentication module (PAM) integration, a home directory is unlocked by its owner's login and stays locked for every other account, including other simultaneously logged-in users. The same mechanism serves a single-user machine where one project directory should be readable only during work on it.

fscrypt does not displace LUKS for the root filesystem, the initial RAM filesystem (initramfs), or swap. Those hold kernel images and paged-out memory that a filesystem-level policy cannot cover, and they are exactly what the "intact kernel" assumption above depends on. Layering is the normal arrangement: **LUKS for the device, an fscrypt policy for the subtree that should stay locked while the device is unlocked.**

Access control after unlocking is unchanged from ordinary files. fscrypt determines **when the key is present**, not which processes touch the plaintext once it is.

**Try next:** on a loopback filesystem (`truncate -s 1G disk.img && mkfs.ext4 -O encrypt disk.img`), apply a policy to a directory, write a file, run `fscrypt lock`, then `cat` the file — the read fails with `ENOKEY` while `ls` still prints base64 ciphertext names.

## Pitfalls

- **`fscrypt encrypt` on a non-empty directory fails.** No conversion path exists; the policy is settable only on an empty directory, so the data must be moved out, the policy applied, and the data copied back in.
- **`lock` reporting success does not mean the plaintext is gone.** An open file descriptor or a shell whose working directory is inside the tree blocks inode eviction; the key is removed but those files stay readable until the last reference closes.
- **A non-root `lock` may leave the key installed.** Under v2 policies each user holds a separate claim, and dropping one claim does not remove a key another user has added.
- **Filenames leak their approximate length.** Padding rounds to 4, 8, 16 or 32 bytes, so a long name remains distinguishable from a short one; file sizes are not padded at all.
- **Deletion works without the key.** No-key names support `unlink` and `rename`, so a locked tree is protected against reading, not against destruction.
- **The `encrypt` feature flag is not free to add later.** `tune2fs -O encrypt` requires the filesystem to be unmounted, which for a root filesystem means booting from other media.
- **Losing the protector loses the data.** The master key is wrapped by the protector; a forgotten passphrase with no second protector registered leaves ciphertext with no recovery route.
