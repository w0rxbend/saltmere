---
title: "uutils coreutils: the Rust rewrite of ls, cp and friends heads into Ubuntu"
date: 2026-08-11
track: linux-tools
summary: "uutils is a from-scratch, MIT-licensed Rust reimplementation of GNU coreutils that now ships by default in Ubuntu. What it is, where it stands on compatibility, the MIT-vs-GPL fight it started, and how to install and test it on any distro today."
reading_time: 5
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

For half a century the tiny programs you run a thousand times a day — `ls`, `cp`, `mv`, `cat`, `sort`, `dd`, `head`, `wc` — have been C code from GNU coreutils. `uutils` is a bet that they don't have to be. It's a from-scratch reimplementation of the whole set in Rust, and after years as a curiosity it has crossed into something bigger: it now ships as the default userland on a mainstream distribution.

## What uutils actually is

uutils is a complete, from-scratch rewrite — not a fork, not a wrapper around the GNU binaries. Every utility is reimplemented in Rust, with the explicit goal of matching GNU behaviour flag-for-flag. Two properties fall out of that choice.

First, it's cross-platform. The same codebase builds and runs on Linux, macOS, the BSDs, **Windows**, and WASI. A `sort` or `cut` that behaves the same on a Linux CI runner and a Windows dev box is genuinely useful, and it's something the GNU tools never targeted.

Second, it's **MIT-licensed**, where GNU coreutils is GPLv3-or-later. That single line in the `LICENSE` file is the source of most of the political heat around the project — more on that below.

The project ships as a BusyBox-style *multicall binary*: one executable named `coreutils` that dispatches to a subcommand, so `coreutils ls -l` runs the `ls` implementation. Symlink that binary to a bare name (`ln -s coreutils ls`) and it behaves like the tool whose name it was invoked under.

## Where it stands in 2026

The current release is **0.10**, tagged 5 August 2026. On the project's own scoreboard — the upstream GNU coreutils test suite, run against the Rust binaries — it passes **93.48%** of tests (645 of 690). That number has climbed steadily: it was in the mid-80s a year earlier. The 0.10 cycle sharpened `date`, `du`, `head`, `tail`, `ls`, `stat` and others, added conveniences like `mv --exchange` (atomic path swap) and `install --reflink`, and closed a batch of time-of-check-to-time-of-use (TOCTOU) races in `touch`, `mkfifo`, `head` and `split`.

93% is close, but the missing 7% is not noise. Real gaps remain in obscure flag combinations, locale and collation edge cases in `sort`, and corner behaviours of `cp`/`mv`/`rm` around symlinks and cross-filesystem moves. If you have a script that leans on a rarely-used GNU flag, test it — don't assume.

## The Ubuntu news hook

Canonical shipped **rust-coreutils as the default in Ubuntu 25.10 "Questing Quokka"** (October 2025), deliberately, to get maximum real-world exposure ahead of the LTS. It stuck: **Ubuntu 26.04 LTS** (April 2026) also defaults to uutils — with one important asterisk. After two rounds of security audit, Canonical judged `cp`, `mv` and `rm` not yet ready because of unresolved TOCTOU concerns, so **those three are still provided by GNU coreutils in 26.04**. The rest of the set is Rust. Full migration, including the last three, is targeted for 26.10.

So the accurate framing as of mid-2026 is: default on Ubuntu, but not yet *all* of it, and not (yet) the default anywhere else. Fedora and others are watching, not shipping.

## The MIT-vs-GPL fight

Replacing the base userland's licence is the part that turned into a genuine debate rather than a changelog note. GNU coreutils is copyleft: derivatives must stay open. uutils is permissive, so a vendor can take it, modify it, and ship a proprietary build with no obligation to release source. Critics — including a long-running [request to relicense under the GPL](https://github.com/uutils/coreutils/issues/2757) and heated threads on the Rust forums — argue that swapping GPL tools for MIT ones quietly erodes the copyleft foothold that has kept the Linux base system open for decades. Defenders counter that permissive licensing is exactly what drives adoption, and that a memory-safe, cross-platform userland is worth it. The maintainers have kept MIT. Wherever you land, it's a real trade-off, not a technicality.

This is the same wave that brought **[sudo-rs](/articles/linux-tools/2026-07-31-sudo-rs-memory-safe)** — the Rust `sudo`/`su` that also became default in Ubuntu 25.10 — and it's fair to read both as one project: oxidising the small, root-adjacent, memory-unsafe C programs that sit at the bottom of every Linux box.

## Try it today, on any distro

You don't need Ubuntu 25.10 to kick the tyres. The crate is on crates.io:

```bash
# Build and install the multicall binary (needs a Rust toolchain)
cargo install coreutils

# The installed binary is `coreutils`; call any util as a subcommand
coreutils ls -l --color=auto
coreutils sort -h < sizes.txt
coreutils --help          # lists every implemented utility

# Prefer bare names? Symlink them into a dir early on your PATH
mkdir -p ~/.local/uutils && cd ~/.local/uutils
for u in ls cp mv cat sort head tail wc; do ln -sf "$(command -v coreutils)" "$u"; done
export PATH="$HOME/.local/uutils:$PATH"
ls --version              # -> "ls (uutils coreutils) 0.10"
```

On Debian/Ubuntu you can instead `apt install rust-coreutils`, which registers the tools through the alternatives system. To measure compatibility yourself, clone the repo and run the GNU suite harness:

```bash
git clone https://github.com/uutils/coreutils && cd coreutils
bash util/build-gnu.sh        # fetches & builds GNU tests against uutils
bash util/run-gnu-test.sh     # runs them; prints pass/fail totals
```

That last step is the honest way to answer "will this break my workflow?" — point it at the utilities you care about and read the failures.

**Try next:** on a throwaway VM or container, symlink the uutils multicall over `ls`, `sort` and `wc`, run your most-used shell scripts against them, then diff the output against the GNU originals — the failures you find are exactly the 7% the compatibility number is warning you about.
