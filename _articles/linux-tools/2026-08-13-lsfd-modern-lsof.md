---
title: "lsfd: the column-oriented lsof replacement in util-linux"
date: 2026-08-13
track: linux-tools
summary: "lsfd ships with util-linux and reads /proc directly. It exposes a filter expression language, selectable output columns and JSON, so 'which process holds this deleted file?' becomes a single predicate rather than a grep pipeline."
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

**Gist.** Answering "which process still holds this unmountable filesystem, this unlinked logfile, or this socket?" traditionally means running `lsof` and reducing a wall of whitespace-delimited text with `awk`. `lsfd` models the same question as a relational one: **every open file descriptor is a row, each row carries many named columns, and a boolean expression selects rows while `-o` projects columns**. The cost of that model is portability — `lsfd` reads `/proc` and nothing else, so it exists only on Linux, and its per-row values are exactly what the kernel chose to expose through `/proc`, no more.

`lsfd` has shipped **as part of util-linux since v2.38** (2022), and later releases up to and including v2.41 have continued to extend it. On a recent distribution it is present without additional installation, because util-linux is part of the base system.

## The model: rows, columns, filters

The unit of the table is the file descriptor, not the process and not the file. A process holding the same inode on three descriptors produces three rows; two processes sharing one inode produce two rows with equal `INODE` values. Available columns include `PID`, `COMMAND`, `FD`, `TYPE`, `NAME`, `INODE`, `SIZE`, `DELETED`, `SOURCE`, `MODE` and `POS`; the full set runs to several dozen and is listed by `-H`. Three flags cover most work:

- `-o` / `--output` selects the projected columns. A leading `+` **appends** to the default set instead of replacing it.
- `-Q` / `--filter` takes a boolean expression **evaluated once per row**; rows for which it is false are not printed.
- `-J` / `--json` emits structured output, and `-H` / `--list-columns` prints every column name the installed build supports.

`-H` matters because the column set is build- and version-dependent. A script that names a column absent from the local build fails at that point rather than silently omitting data.

```bash
lsfd -H | head                     # enumerate the column names of this build
lsfd -o PID,COMMAND,FD,TYPE,NAME   # explicit, minimal projection
lsfd -o +DELETED,SOURCE            # default columns plus two
```

Filter expressions are written over column names, with comparison and regular-expression operators (`==`, `!=`, `=~`) combined by `and` and `or`. Because the predicate is evaluated against typed columns rather than against rendered text, a filter on `TYPE` cannot be confused by a pathname that happens to contain the word `SOCK`:

```bash
# every socket on the machine
lsfd -Q 'TYPE == "SOCK"'

# non-regular files held by pid 1 or pid 2
lsfd -Q '(PID == 1 or PID == 2) and TYPE != "REG"'

# descriptors whose path matches a regular expression
lsfd -Q 'NAME =~ ".*/dconf/.*"'
```

## The unlinked-file query

The canonical case: a service rotated its log, the directory entry is gone, but a process still holds the inode open. **A file's blocks are released only when both its link count and its open-descriptor count reach zero**, so the space stays allocated and `du` — which walks directory entries — cannot see it, while `df` still reports it as used. That discrepancy is the symptom.

`DELETED` is a boolean column expressing reachability from the filesystem namespace, so it is a predicate rather than a string to grep for:

```bash
# every open-but-unlinked descriptor, with owner and retained size
lsfd -Q 'DELETED' -o PID,COMMAND,FD,SIZE,NAME
```

Two remedies follow from the reference-count rule. Terminating the holder closes its descriptors and drops the count to zero. Alternatively `truncate -s0 /proc/<pid>/fd/<fd>` reclaims the blocks while the descriptor stays open — but truncation does not move the offset held by the open file description, so a writer that had reached offset *N* continues writing at *N* and the file regains a hole of that length.

## Sockets as columns rather than as text

For socket descriptors `lsfd` populates dedicated socket columns — `SOCK.PROTONAME`, `SOCK.STATE` and `SOCK.LISTENING` among them — instead of leaving that detail encoded in the rendered `NAME` string. Protocol-specific columns exist alongside them for addresses; `-H` names those the local build provides:

```bash
lsfd -Q 'TYPE == "SOCK"' \
  -o PID,COMMAND,FD,SOCK.PROTONAME,SOCK.STATE,SOCK.LISTENING,NAME
```

The result joins socket-level detail of the kind `ss` reports to the owning process, in a single pass over `/proc`.

## lsof compared with lsfd

| Concern            | lsof                          | lsfd                              |
|--------------------|-------------------------------|-----------------------------------|
| Data source        | portable, many backends       | `/proc` only (Linux-specific)     |
| Output             | fixed columns, `-F` scripting | named columns via `-o`, `+append` |
| Filtering          | flags (`-p`, `-i`, `-u`)      | expression language via `-Q`      |
| Machine-readable   | `-F` field mode               | JSON via `-J`                     |
| Ships in           | separate package              | util-linux (base system)          |

`lsof` remains the only option on the BSDs and AIX, where `/proc` in the Linux form is absent or differently shaped. The trade is explicit: `lsfd` gives up every non-Linux target in exchange for a single, typed data source.

## Machine-readable output

`-J` removes the parsing step rather than making it easier. The following aggregates descriptors per process and reports the five largest holders:

```bash
lsfd -J -o PID,COMMAND | \
  jq -r '.lsfd | group_by(.pid)
         | map({pid: .[0].pid, cmd: .[0].command, fds: length})
         | sort_by(-.fds)[:5][] | "\(.fds)\t\(.pid)\t\(.cmd)"'
```

Grouping is required because the row unit is the descriptor: process count is `length` of a group, never a row count.

## Pitfalls

- **A row is a descriptor, not a file.** Summing `SIZE` over rows double-counts an inode held on several descriptors, and inflates totals when several processes share one file.
- **An unprivileged run silently under-reports.** `/proc/<pid>/fd` is readable only by the owning user and by root, so a non-root invocation omits or degrades rows for other users' processes; the output is not marked as partial.
- **The scan is not atomic.** `lsfd` walks `/proc` process by process; a descriptor opened or closed during the walk may or may not appear, and a process that exits mid-walk leaves no rows at all. Two runs of the same filter can legitimately disagree.
- **Column availability varies by build and version.** Naming a column that this build does not provide is an error, not an empty field — check with `-H` before hard-coding a projection in a script.
- **Filtering on `NAME` is filtering on a rendered string.** A regular expression over `NAME` matches whatever text `lsfd` printed for that row, including annotations; typed columns such as `TYPE` or `DELETED` are the stable predicates.
- **`truncate` on `/proc/<pid>/fd/<fd>` does not reset the writer's offset.** The blocks are freed, then the next write lands at the previous offset and recreates a hole of that size.
- **No directory walk can find a pinned inode.** An unlinked file has no directory entry, so `du` and `find` report nothing however thorough the search; the space shows only in `df` totals and in the descriptor table.
