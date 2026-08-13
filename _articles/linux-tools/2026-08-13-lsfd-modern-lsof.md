---
title: "lsfd: the column-oriented lsof replacement hiding in util-linux"
date: 2026-08-13
track: linux-tools
summary: "lsfd ships with util-linux and reads /proc directly. It gives you a filter expression language, selectable output columns, and JSON — so 'who holds this deleted file?' becomes a one-line query instead of a grep pipeline."
reading_time: 5
tags: [lsfd, lsof, util-linux, proc, sockets, linux-tools]
sources:
  - title: "lsfd(1) — Linux manual page (man7.org)"
    url: "https://man7.org/linux/man-pages/man1/lsfd.1.html"
  - title: "RFC: lsfd, a brand new Linux specific replacement for lsof (util-linux PR #1418)"
    url: "https://github.com/util-linux/util-linux/pull/1418"
  - title: "lsfd(1) — Arch manual pages"
    url: "https://man.archlinux.org/man/core/util-linux/lsfd.1.en"
  - title: "util-linux v2.41 Release Notes"
    url: "https://github.com/util-linux/util-linux/blob/master/Documentation/releases/v2.41-ReleaseNotes"
  - title: "lsfd — ManKier"
    url: "https://www.mankier.com/1/lsfd"
---

`lsof` is the tool you reach for when a filesystem won't unmount, a deleted logfile is still eating disk, or you need to know which process owns a socket. It's also a portable C program that predates `/proc`, carries decades of cross-Unix abstraction, and emits a wall of whitespace-delimited text you then have to `awk` apart. `lsfd` is the Linux-only answer: it reads `/proc` and nothing else, and it treats file descriptors as rows in a table you can filter and project.

It's not a side project — `lsfd` has shipped **as part of util-linux since v2.38** (2022), and the current line is **2.41.x** (2.41.3 is what Ubuntu 26.04 carries). If you have a recent distro, it's already installed.

## The model: rows, columns, filters

Every open file descriptor on the system is a row. Each row has ~50 available columns — `PID`, `COMMAND`, `FD`, `TYPE`, `NAME`, `INODE`, `SIZE`, `DELETED`, `SOURCE`, `MODE`, `POS`, and more. Three flags cover most work:

- `-o` / `--output` picks columns. Prefix with `+` to *append* to the defaults.
- `-Q` / `--filter` takes a boolean expression evaluated per row.
- `-J` / `--json` emits structured output; `-H` / `--list-columns` prints every column name.

```bash
lsfd -H | head              # discover the column names
lsfd -o PID,COMMAND,FD,TYPE,NAME   # a lean, explicit view
lsfd -o +DELETED,SOURCE            # defaults plus two extras
```

The filter language is the real upgrade. Expressions use column names, comparison and regex operators (`==`, `!=`, `=~`), and `and`/`or`:

```bash
# every socket on the box
lsfd -Q 'TYPE == "SOCK"'

# non-regular files held by pid 1 or pid 2
lsfd -Q '(PID == 1 or PID == 2) and TYPE != "REG"'

# anything whose path matches a regex
lsfd -Q 'NAME =~ ".*/dconf/.*"'
```

## The classic: who is holding a deleted file?

This is the query that justifies the tool on its own. A service rotated its log, the inode is unlinked, but a process still has it open, so the space never comes back. With `lsfd` it's one predicate:

```bash
# every open-but-unlinked file, with the PID and how big it still is
lsfd -Q 'DELETED' -o PID,COMMAND,FD,SIZE,NAME
```

`DELETED` is a boolean column — reachability from the filesystem — so you filter on it directly instead of grepping for a `(deleted)` suffix. Add `SIZE` and you immediately see which zombie descriptor is worth killing. Restart or `truncate -s0 /proc/<pid>/fd/<fd>` to reclaim.

## Sockets without the guesswork

`lsfd` decodes socket state into dedicated columns rather than cramming it into `NAME`. The `sock.*` columns expose protocol, local/remote addresses, and listening state:

```bash
lsfd -Q 'TYPE == "SOCK"' \
  -o PID,COMMAND,FD,SOCK.PROTONAME,SOCK.STATE,SOCK.LISTENING,NAME
```

That's `ss`-grade detail joined to the owning process, in one pass.

## lsof vs lsfd at a glance

| Concern            | lsof                         | lsfd                              |
|--------------------|------------------------------|-----------------------------------|
| Data source        | portable, many backends      | `/proc` only (Linux-specific)     |
| Output             | fixed columns, `-F` scripting| named columns via `-o`, `+append` |
| Filtering          | flags (`-p`, `-i`, `-u`)     | expression language via `-Q`      |
| Machine-readable   | `-F` field mode              | real JSON via `-J`                |
| Ships in           | separate package             | util-linux (base system)          |

## Wire it into scripts

JSON output means you skip the parsing entirely. Feed it to `jq`:

```bash
# top 5 processes by number of open fds
lsfd -J -o PID,COMMAND | \
  jq -r '.lsfd | group_by(.pid)
         | map({pid: .[0].pid, cmd: .[0].command, fds: length})
         | sort_by(-.fds)[:5][] | "\(.fds)\t\(.pid)\t\(.cmd)"'
```

`lsof` isn't going away — it runs on the BSDs and AIX where `lsfd` never will. But on a Linux box, for the questions you actually ask under pressure, the expression filter and `-o` projection make `lsfd` faster to drive and easier to trust.

**Try next:** run `lsfd -Q 'DELETED' -o PID,COMMAND,SIZE,NAME` on a long-running server and see how much disk is pinned by open-but-unlinked files you didn't know about.
