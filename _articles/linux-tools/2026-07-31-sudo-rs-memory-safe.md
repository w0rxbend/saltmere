---
title: "sudo-rs: the memory-safe sudo that's now default in Ubuntu 25.10"
date: 2026-07-31
track: linux-tools
summary: "Ubuntu 25.10 ships sudo-rs — a Rust rewrite of sudo/su that drops rarely-used features to shrink the attack surface. What it keeps, what it drops, who funds it, and how to run it."
reading_time: 4
tags: [sudo, rust, security, memory-safety, ubuntu, linux-tools]
sources:
  - title: "sudo-rs — GitHub (Trifecta Tech Foundation)"
    url: "https://github.com/trifectatechfoundation/sudo-rs"
  - title: "sudo and su — Prossimo / ISRG initiative page"
    url: "https://www.memorysafety.org/initiative/sudo-su/"
  - title: "sudo-rs Headed to Ubuntu — Prossimo"
    url: "https://www.memorysafety.org/blog/sudo-rs-headed-to-ubuntu/"
  - title: "sudo-rs is now default for Questing Quokka — Ubuntu Discourse"
    url: "https://discourse.ubuntu.com/t/sudo-rs-is-now-default-for-questing-quokka/66497"
  - title: "sudo-rs Is Now The Default sudo Of Ubuntu 25.10 — Phoronix"
    url: "https://www.phoronix.com/news/Ubuntu--Now-Default-sudo-rs"
---

`sudo` is a ~30-year-old C program that runs as root on nearly every Linux box. That combination — root privileges plus a memory-unsafe language plus decades of accreted features — is exactly the kind of target that keeps producing CVEs. `sudo-rs` is a from-scratch reimplementation of `sudo` and `su` in Rust, and as of October 2025 it is the **default** on Ubuntu 25.10 "Questing Quokka."

## Why rewrite it

Two reasons, and the second matters more than people expect.

The obvious one is memory safety: Rust eliminates the buffer overflows and use-after-frees that have historically bitten `sudo`. The subtler one is **attack-surface reduction by omission**. The `sudo-rs` authors deliberately did *not* reimplement rarely-used features. That decision paid off in July 2025 when two upstream `sudo` bugs landed — **CVE-2025-32462** (the `-h`/host option) and **CVE-2025-32463** (a critical `chroot` flaw). Both lived in features that `sudo-rs` never implemented, so `sudo-rs` was unaffected. Less code is less to exploit.

## What it keeps — and what it drops

`sudo-rs` is designed as a drop-in for the common case: it reads `/etc/sudoers` and `/etc/sudoers.d/*`, honours user/group rules, `NOPASSWD`, per-command allowlists, `runas` specs, `secure_path`, and `sudoedit`. It authenticates exclusively through **PAM**.

What it intentionally leaves out:

- **LDAP sudoers** (`sudoers.ldap`, `cvtsudoers`, SSSD backends) — use LDAP auth via PAM instead
- **I/O logging** and `sudoreplay`
- **`INTERCEPT`** shell-escape prevention
- **sendmail** integration (`mail_badpass`, `mailto`)
- **File-based logging** — everything goes to syslog

It also hardens some defaults that upstream leaves configurable: `env_reset` is always on, `visiblepw` is always off, and `use_pty` is on by default. Directives it doesn't understand (like `requiretty`) are ignored for compatibility rather than fatal.

## Who's paying for it

`sudo-rs` was started and funded by the **Internet Security Research Group** — the Let's Encrypt people — through its **Prossimo** memory-safety project. Prossimo commissioned the work from **Tweede Golf** and **Ferrous Systems**. Stewardship later moved to the **Trifecta Tech Foundation**, which now maintains it with backing from the NLnet Foundation, the Sovereign Tech Agency, and Canonical. Two independent security audits have been completed.

## Trying it

On Debian/Ubuntu, Fedora, or Arch:

```bash
sudo apt install sudo-rs        # Debian/Ubuntu
sudo dnf install sudo-rs        # Fedora
sudo pacman -S sudo-rs          # Arch
```

Confirm you're running the Rust build — the version string names it explicitly:

```bash
$ sudo --version
sudo-rs 0.2.x
```

A `sudoers` snippet it honours cleanly (drop it in as a file, not by editing the main file):

```
# /etc/sudoers.d/deploy
%deploy   ALL=(ALL:ALL) NOPASSWD: /usr/bin/systemctl restart myapp
Defaults:%deploy timestamp_timeout=15
```

That grants the `deploy` group a single passwordless command and a 15-minute credential cache. Note one compatibility quirk: `sudo-rs` only honours wildcards in **argument** positions, so glob tricks in the command path won't match the way they might upstream.

## The default switch in Ubuntu 25.10

Ubuntu 25.10 (released October 2025) shipped `sudo-rs` as the default `sudo` and `su`, announced on the Ubuntu Discourse on 2 September 2025. Both binaries coexist via `update-alternatives`, so you can flip back if a missing feature bites:

```bash
# See / choose interactively
sudo update-alternatives --config sudo

# Force the Rust build, or revert to the original C sudo
sudo update-alternatives --set sudo /usr/bin/sudo-rs
sudo update-alternatives --set sudo /usr/bin/sudo.ws
```

25.10 is deliberately a proving ground: Canonical wants to shake out bugs before the next LTS reaches tens of millions of long-support users.

**Try next:** on a 25.10 VM, run `sudo --version` to confirm the Rust build, then `sudo update-alternatives --config sudo` and diff the behaviour of your existing `/etc/sudoers.d/*` rules against the C `sudo.ws` — the fastest way to find any feature you actually depend on.
