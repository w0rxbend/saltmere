---
title: "sudo-rs: the memory-safe sudo now default in Ubuntu 25.10"
date: 2026-07-31
track: linux-tools
summary: "Ubuntu 25.10 ships sudo-rs — a Rust reimplementation of sudo and su that omits rarely-used features to shrink the attack surface. What it keeps, what it drops, who funds it, and how it is deployed."
reading_time: 5
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

**Gist.** `sudo` is a decades-old C program that executes with root privileges on nearly every Linux installation, and that combination — full privilege, a memory-unsafe language, and decades of accreted optional features — has produced a steady stream of Common Vulnerabilities and Exposures (CVE) entries. `sudo-rs` is a from-scratch reimplementation of `sudo` and `su` in Rust that both removes the memory-unsafety class and **declines to reimplement rarely-used features**, and it became the default on Ubuntu 25.10 "Questing Quokka" in October 2025. The cost of the second decision is compatibility: any deployment depending on an omitted feature — Lightweight Directory Access Protocol (LDAP) sudoers, input/output (I/O) logging, file-based logging — has no equivalent under `sudo-rs` and must fall back to the C implementation.

## The two independent safety arguments

The first argument is language-level. Rust's ownership and borrowing rules rule out, in safe code, the buffer overflows and use-after-free defects that have historically affected `sudo`. This class of bug is excluded by the compiler rather than caught by review — with the caveat that the exclusion covers safe Rust only, not `unsafe` blocks or the C libraries the program links against, PAM among them.

The second argument is **surface reduction by omission**, and it is the one that has been demonstrated concretely. The `sudo-rs` authors deliberately did not port features they considered rarely used. In mid-2025 two upstream `sudo` vulnerabilities were published — **CVE-2025-32462**, in the `-h`/host option handling, and **CVE-2025-32463**, a critical flaw in the `chroot` path. Both defects resided in code paths that `sudo-rs` had never implemented, so `sudo-rs` was not affected. **Code that does not exist cannot be exploited**, and this outcome is independent of the implementation language.

## What is preserved

`sudo-rs` targets drop-in behaviour for the common configuration. It parses `/etc/sudoers` and the `/etc/sudoers.d/*` drop-in directory, and honours user and group rules, `NOPASSWD`, per-command allowlists, `runas` specifications, `secure_path`, and `sudoedit`. Authentication runs **exclusively through Pluggable Authentication Modules (PAM)**; there is no alternative authentication backend to select.

## What is omitted

- **LDAP sudoers** — `sudoers.ldap`, `cvtsudoers`, and System Security Services Daemon (SSSD) backends. Directory-backed *authentication* remains available through PAM; directory-backed *policy* does not.
- **I/O logging** and the `sudoreplay` session-replay tool.
- **`INTERCEPT`**, the shell-escape prevention mechanism.
- **sendmail integration**, including `mail_badpass` and `mailto`.
- **File-based logging** — all log output goes to syslog.

Several upstream-configurable defaults are fixed rather than configurable: **`env_reset` is always on**, **`visiblepw` is always off**, and **`use_pty` is on by default**. The parser's handling of unknown input is permissive: directives it does not recognise, such as `requiretty`, are **ignored rather than treated as fatal**. This is the load-bearing compatibility property — an existing `sudoers` file containing unsupported directives still loads, which means an omitted feature manifests as silently absent behaviour rather than as a parse error.

## Funding and stewardship

`sudo-rs` was initiated and funded by the **Internet Security Research Group (ISRG)**, the organisation behind the Let’s Encrypt certificate authority, through its **Prossimo** memory-safety programme. The implementation work was carried out by **Tweede Golf** and **Ferrous Systems**. Stewardship subsequently moved to the **Trifecta Tech Foundation**, which maintains the project. **An independent security audit has been completed**; the audit report is linked from the project's own documentation.

## Deployment

A `sudo-rs` package is available in several distributions, under that name:

```bash
sudo apt install sudo-rs        # Debian/Ubuntu
sudo dnf install sudo-rs        # Fedora
sudo pacman -S sudo-rs          # Arch
```

The version string names the Rust build explicitly, which distinguishes it from the C implementation:

```bash
$ sudo --version
sudo-rs 0.2.x
```

A drop-in policy file of the following form parses cleanly — placed as a separate file rather than by editing the main `sudoers`:

```
# /etc/sudoers.d/deploy
%deploy   ALL=(ALL:ALL) NOPASSWD: /usr/bin/systemctl restart myapp
```

That grants the `deploy` group one passwordless command. One documented divergence applies: **`sudo-rs` honours wildcards only in argument positions**, so a glob in the command path does not match as it may under upstream `sudo`.

## The Ubuntu 25.10 default switch

Ubuntu 25.10, released in October 2025, ships `sudo-rs` as the default `sudo` and `su`. The change was announced on the Ubuntu Discourse ahead of the release. Both implementations coexist through `update-alternatives`, so the selection is reversible per system without removing a package:

```bash
# Inspect or choose interactively
sudo update-alternatives --config sudo

# Select the Rust build, or revert to the C implementation
sudo update-alternatives --set sudo /usr/bin/sudo-rs
sudo update-alternatives --set sudo /usr/bin/sudo.ws
```

The C implementation remains installed under the name `sudo.ws`. An interim release such as 25.10 carries a shorter support window than a Long Term Support (LTS) release; the next LTS is the point at which the change would reach long-lived deployments.

## Pitfalls

- **An unsupported `Defaults` directive does not fail loudly.** Because unknown directives are ignored rather than fatal, a policy relying on `INTERCEPT` or on `mail_badpass` parses successfully and the intended restriction or notification silently does not happen.
- **Removing I/O logging removes `sudoreplay` evidence.** A host switched to `sudo-rs` stops producing session recordings; an audit process that reads them finds an empty archive with no error at the point of the switch.
- **A wildcard in the command path stops matching.** A rule whose executable path contains a glob matches under upstream `sudo` but not under `sudo-rs`, so the affected users are denied rather than over-permitted — the failure is a lockout, not a privilege escalation.
- **LDAP-backed policy and LDAP-backed authentication are different things.** Authentication continues to work through PAM after the switch, which can mask the fact that `sudoers.ldap` rules are no longer being consulted at all.
- **File-based log destinations are silently unused.** With all output routed to syslog, a log-shipping pipeline tailing a `sudo` logfile receives nothing.
- **Checking `sudo --version` on the wrong path proves nothing.** With `update-alternatives` in play, the binary invoked depends on `PATH` resolution, so the version string must be read from the same invocation the policy under test uses.
