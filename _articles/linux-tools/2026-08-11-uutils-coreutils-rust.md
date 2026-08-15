---
title: "uutils coreutils: the Rust reimplementation of ls, cp and friends enters Ubuntu"
date: 2026-08-11
track: linux-tools
summary: "uutils is a from-scratch, MIT-licensed Rust reimplementation of GNU coreutils that now ships by default in Ubuntu. Its scope, its measured compatibility with the GNU test suite, the licensing dispute it provoked, and how to install and test it on any distribution."
reading_time: 6
tags: [uutils, coreutils, rust, ubuntu, memory-safety, linux-tools]
sources:
  - title: "uutils/coreutils — Cross-platform Rust rewrite of the GNU coreutils (GitHub)"
    url: "https://github.com/uutils/coreutils"
  - title: "Rust-Based uutils Coreutils 0.10 Reaches 93.5% GNU Compatibility — Linuxiac"
    url: "https://linuxiac.com/rust-based-uutils-coreutils-0-10-reaches-93-5-gnu-compatibility/"
  - title: "An update on rust-coreutils — Ubuntu Community Hub"
    url: "https://discourse.ubuntu.com/t/an-update-on-rust-coreutils/80773"
  - title: "rust-coreutils status — LWN.net"
    url: "https://lwn.net/Articles/1069593/"
  - title: "Please switch to the GPL as your software license — uutils/coreutils issue #2757"
    url: "https://github.com/uutils/coreutils/issues/2757"
---

**Gist.** The base userland of a Linux system — `ls`, `cp`, `mv`, `cat`, `sort`, `dd`, `head`, `wc` — has been GNU coreutils C code for decades, and that code carries the memory-safety exposure of manual pointer and buffer handling. uutils reimplements the same command set from scratch in Rust, matching GNU behaviour flag for flag, and now ships as the default userland on Ubuntu. The cost is compatibility: release **0.10** passes **93.48%** of the upstream GNU test suite (**645 of 690 tests**), so the remaining fraction is a set of behavioural differences concentrated in less common flags, locale handling and symbolic-link corner cases.

## Scope of the reimplementation

uutils is a complete rewrite, not a fork of the GNU sources and not a wrapper that shells out to GNU binaries. Each utility is written afresh in Rust, with the stated goal of matching GNU behaviour flag for flag. Two properties follow from that structure.

The first is portability. The same codebase builds and runs on Linux, macOS, the BSDs, **Windows**, and WASI (the WebAssembly System Interface). A `sort` or `cut` invocation therefore has one implementation across a Linux continuous-integration runner and a Windows developer machine; GNU coreutils does not target Windows.

The second is licensing. uutils is **MIT-licensed**, whereas GNU coreutils is GPLv3-or-later (GNU General Public License, version 3 or later). That difference is the subject of the dispute described below.

The project ships as a BusyBox-style **multicall binary**: a single executable named `coreutils` that dispatches on a subcommand, so `coreutils ls -l` runs the `ls` implementation. The binary also inspects the name it was invoked under, so a symbolic link named `ls` pointing at the multicall binary behaves as `ls`. This is the mechanism that allows the Rust set to be substituted for the GNU set without altering any caller: **the dispatch key is `argv[0]`**, and every caller in a shell script supplies that implicitly.

## Measured state at release 0.10

The current release is **0.10**, tagged **5 August 2026**. The project's compatibility figure is produced by building the upstream GNU coreutils test suite and running it against the Rust binaries rather than the GNU ones: **645 of 690 tests pass, 93.48%**. The figure was in the mid-eighties a year earlier.

The 0.10 cycle improved `date`, `du`, `head`, `tail`, `ls` and `stat` among others, added `mv --exchange` (an atomic swap of two paths) and `install --reflink`, and closed a batch of **time-of-check-to-time-of-use (TOCTOU)** races in `touch`, `mkfifo`, `head` and `split`. A TOCTOU race is the interval between a program checking a property of a path — that it exists, that it is a regular file, that it is owned by the caller — and acting on that path. If an attacker replaces the path with a symbolic link inside that interval, the action lands on a target the check never approved. The defence is to eliminate the interval by operating on a file descriptor obtained once, rather than re-resolving the name.

The residual **6.52%** is not uniform noise. The gaps reported are in **uncommon flag combinations, locale and collation behaviour in `sort`, and the corner cases of `cp`, `mv` and `rm` around symbolic links and cross-filesystem moves**. Which of those gaps a given system meets is not derivable from the aggregate figure; the test-suite harness described below reports them by name.

## Deployment status in Ubuntu

Canonical shipped **rust-coreutils as the default in Ubuntu 25.10 "Questing Quokka"** (October 2025) in order to obtain real-world exposure before the long-term-support release. **Ubuntu 26.04 LTS** (April 2026) also defaults to uutils, with one exclusion: after two rounds of security audit, Canonical judged `cp`, `mv` and `rm` not yet ready owing to unresolved TOCTOU concerns, so **those three utilities are still supplied by GNU coreutils in 26.04**. The remainder of the set is the Rust implementation. Full migration, including the three excluded utilities, is targeted for 26.10.

The accurate statement as of mid-2026 is therefore: default on Ubuntu, not the whole set, and not the default on any other distribution. Fedora and others have not shipped it.

## The licensing dispute

Replacing the base userland's licence is the part of the change that produced sustained argument rather than a changelog entry. GNU coreutils is copyleft: a derivative work must be distributed under the same terms, so modifications remain available in source form. uutils is permissive: a vendor may take the code, modify it, and ship a proprietary build with no obligation to release source.

Critics — including a long-running [request to relicense under the GPL](https://github.com/uutils/coreutils/issues/2757) — argue that substituting MIT-licensed tools for GPL ones removes copyleft from the base system. Defenders argue that permissive licensing widens adoption and that a memory-safe, cross-platform userland justifies the change. The maintainers have retained MIT. The trade-off is substantive rather than procedural.

The same pattern produced **[sudo-rs](/articles/linux-tools/2026-07-31-sudo-rs-memory-safe)**, the Rust implementation of `sudo` and `su` that also became the Ubuntu 25.10 default. Both replace small C programs that run with or adjacent to root privilege.

## Installing and testing on any distribution

The crate is published on crates.io, so Ubuntu is not required to evaluate the tools.

```bash
# Build and install the multicall binary (requires a Rust toolchain)
cargo install coreutils

# The installed binary is `coreutils`; each utility is a subcommand
coreutils ls -l --color=auto
coreutils sort -h < sizes.txt
coreutils --help          # lists every implemented utility

# Bare names require symlinks in a directory early on PATH
mkdir -p ~/.local/uutils && cd ~/.local/uutils
for u in ls cp mv cat sort head tail wc; do ln -sf "$(command -v coreutils)" "$u"; done
export PATH="$HOME/.local/uutils:$PATH"
ls --version              # names uutils coreutils, not GNU coreutils
```

On Debian and Ubuntu, `apt install rust-coreutils` registers the tools through the alternatives system instead. The compatibility figure can be reproduced locally with the project's own harness:

```bash
git clone https://github.com/uutils/coreutils && cd coreutils
bash util/build-gnu.sh        # fetches and builds the GNU tests against uutils
bash util/run-gnu-test.sh     # runs them; prints pass/fail totals
```

Running that harness, and then running the workload's own scripts against symlinked uutils binaries on a disposable container, converts the aggregate 93.48% into the specific list of failures that apply to one system.

## Pitfalls

- **Assuming the whole Ubuntu 26.04 LTS userland is Rust.** `cp`, `mv` and `rm` are still GNU coreutils there, so a bug reproduced on 26.04 against those three is a GNU bug, and behaviour observed for them will change again at 26.10.
- **Reading 93.48% as "6.52% of invocations differ".** The figure counts GNU test-suite cases, not weighted real usage; a script may exercise only passing behaviour, or may sit entirely inside a failing case.
- **Trusting `sort` output to be byte-identical across the switch.** Locale and collation handling is one of the named gap areas, so a pipeline that depends on a specific ordering under a non-C locale can produce a different order without any error.
- **Symlinking the multicall binary under a name it does not implement.** Dispatch is by `argv[0]`; an unimplemented name fails at invocation rather than falling through to the GNU binary still on `PATH`.
- **Treating the closed TOCTOU races as a general guarantee.** The 0.10 notes cover `touch`, `mkfifo`, `head` and `split`; Canonical's audit left `cp`, `mv` and `rm` outstanding, which is why those three were excluded from the default.
- **Deploying via `cargo install` and expecting distribution updates.** A crates.io build is outside the package manager, so it receives no security updates from the distribution and shadows the packaged tools for every process that inherits the modified `PATH`.
