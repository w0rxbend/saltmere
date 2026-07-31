---
title: "fscrypt: encrypt one directory, not the whole disk"
date: 2026-07-31
track: linux-tools
summary: "LUKS encrypts a block device — all or nothing, one key, unlocked at boot. fscrypt is built into ext4 and f2fs and works per-directory, so a single folder can be encrypted with its own key that's only present when you're using it. Here's how to turn it on."
reading_time: 5
tags: [fscrypt, encryption, ext4, f2fs, security]
sources:
  - title: "Filesystem-level encryption (fscrypt) — Linux Kernel documentation"
    url: "https://www.kernel.org/doc/html/latest/filesystems/fscrypt.html"
  - title: "google/fscrypt — high-level management tool (policies, protectors, PAM)"
    url: "https://github.com/google/fscrypt"
  - title: "fscrypt — ArchWiki"
    url: "https://wiki.archlinux.org/title/Fscrypt"
---

Full-disk encryption with LUKS is the right default, but it's coarse: one passphrase unlocks one block device at boot, and from then on everything is plaintext to anyone with access to the running system. Sometimes you want something finer — *this* directory of secrets is encrypted with *its* key, and that key isn't in the kernel unless you've explicitly unlocked it, even though the rest of the filesystem is normal. That's **fscrypt**: encryption implemented *inside* ext4, f2fs and UBIFS, at directory granularity.

## What it actually encrypts

fscrypt protects **file contents and file names** within a designated directory tree. By default it uses AES-256-XTS for contents and AES-256-CTS-CBC for names (low-power devices can pick the Adiantum cipher instead). What it does *not* hide is metadata like the directory structure's existence, file sizes, or timestamps — if your threat model needs those hidden too, fscrypt alone isn't enough. Each encrypted tree is governed by a *policy* tied to a key; without the key in the kernel keyring, `ls` shows the directory but the filenames are ciphertext and the contents are unreadable.

## Turning it on

The filesystem needs the `encrypt` feature flag. On an existing ext4 volume:

```bash
sudo tune2fs -O encrypt /dev/sdaN     # unmounted, or set at mkfs time
```

Then use Google's `fscrypt` tool, which handles the key management ("protectors") and the kernel plumbing for you:

```bash
sudo fscrypt setup                    # one-time: prepare /etc/fscrypt.conf + metadata
sudo fscrypt setup /mnt/data          # prepare this filesystem's mountpoint

mkdir /mnt/data/secrets
fscrypt encrypt /mnt/data/secrets     # choose a protector (passphrase, or your login)
```

At `encrypt` time the directory must be empty — you encrypt first, then put files in. From here the flow is:

```bash
fscrypt status /mnt/data/secrets      # locked or unlocked? which protector?
fscrypt lock   /mnt/data/secrets      # evict the key; contents become ciphertext
fscrypt unlock /mnt/data/secrets      # re-supply the passphrase to read again
```

When locked, the files are still *there* on disk — you just can't read names or contents until you unlock. If you'd rather avoid the Go tool and its config, `fscryptctl` is a minimal C helper that speaks the raw kernel ioctls, leaving key handling entirely to you.

## Where it beats LUKS, and where it doesn't

The killer feature is **per-user keys on a shared machine**: fscrypt integrates with PAM so a user's home directory unlocks with their login password and stays locked for everyone else, including other logged-in users — something a single LUKS volume can't express. It's also nice for a laptop where you want most of the disk fast and unencrypted but one project folder protected, unlocked only when you're working on it.

It is *not* a replacement for full-disk encryption of the swap partition or the root filesystem — those still want LUKS, because fscrypt can't protect the kernel, initramfs, or metadata. The two compose well: LUKS for the device, fscrypt for the extra per-directory key on top. And mind the same-key detail: once a directory is unlocked, any process running as a user who can read it can read it, exactly like normal files — fscrypt controls *when the key is present*, not who touches the plaintext afterward.

**Try next:** on a spare ext4 filesystem (a loopback file works: `truncate -s 1G disk.img && mkfs.ext4 -O encrypt disk.img`), run `fscrypt encrypt` on a folder, drop a file in, `fscrypt lock` it, and `cat` the file — you'll see the read fail while the directory listing shows ciphertext names. That failed `cat` is the whole feature, demonstrated.
