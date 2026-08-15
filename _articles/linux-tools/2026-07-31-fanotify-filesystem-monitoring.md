---
title: "fanotify: watching a whole filesystem instead of one directory at a time"
date: 2026-07-31
track: linux-tools
summary: "inotify requires recursing a tree and registering every directory by hand. fanotify marks an entire filesystem with one call, reports events by file handle, and can gate opens for antivirus-style scanning. A working FID-based watcher and the kernel versions each flag landed in."
reading_time: 6
tags: [linux, fanotify, inotify, filesystem, kernel, c, observability]
sources:
  - title: "fanotify(7) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man7/fanotify.7.html"
  - title: "fanotify_init(2) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man2/fanotify_init.2.html"
  - title: "fanotify_mark(2) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man2/fanotify_mark.2.html"
  - title: "fanotify: add support for more event types (LWN.net)"
    url: "https://lwn.net/Articles/776431/"
  - title: "fatrace — report system-wide file access events (GitHub)"
    url: "https://github.com/martinpitt/fatrace"
---

**Gist.** An inotify watch covers exactly one directory, so whole-tree monitoring means walking the tree, registering each directory, racing against files created during the walk, and re-registering as new directories appear — one watch descriptor per directory, charged against `fs.inotify.max_user_watches`. fanotify replaces the recursion with **a single mark on a mount or on an entire superblock**, and can report events by **file handle** rather than by path. The cost is volume and privilege: a filesystem mark on a busy root filesystem delivers every matching event on the machine, filtering happens in userspace, and a mount or filesystem mark requires `CAP_SYS_ADMIN`.

fanotify was introduced in Linux 2.6.36 and enabled in 2.6.37. Beyond wide marks, it does two things inotify cannot: in its file-identifier mode it reports events by file handle, giving an identity that survives renames, and it can issue **permission events** that let a userspace daemon allow or deny an open before the syscall completes.

## Mount marks versus filesystem marks

Two mark types provide whole-tree coverage without recursion.

- `FAN_MARK_MOUNT` — covers everything reached *through one mount point*. A bind mount, or a second mount of the same filesystem, is a blind spot: accesses arriving through the other mount carry a different mount and do not match the mark.
- `FAN_MARK_FILESYSTEM` (since **Linux 4.20**) — covers the whole superblock regardless of how many mount points expose it. This is the primitive that makes "monitor the entire filesystem" a single call.

The invariant that makes the wide mark usable is that **the mark is attached to the object, not to a path**. Nothing in the watcher has to be updated when directories are created, renamed, or newly mounted over; there is no walk to race against, and no per-directory descriptor limit to exhaust.

## Reporting by file handle: FAN_REPORT_FID

The original notification mode returns an **open file descriptor per event**. That is expensive at high event rates, it consumes descriptors from the reader's table, and for directory-entry events it does not identify *which* entry changed. The file-identifier (FID) path replaces the descriptor:

- `FAN_REPORT_FID` (since **Linux 5.1**) — events carry an `fsid` plus a `file_handle` instead of a file descriptor.
- `FAN_CREATE`, `FAN_DELETE`, `FAN_MOVE` (since **Linux 5.1**) — the directory-entry events that make fanotify a viable inotify replacement.
- `FAN_REPORT_DIR_FID` and `FAN_REPORT_NAME` (both since **Linux 5.9**). `FAN_REPORT_DFID_NAME` is the synonym for the pair and yields the *parent directory* handle together with the entry name.

Under `FAN_REPORT_DFID_NAME` the reader resolves the parent directory with `open_by_handle_at(2)` and takes the name from the event record. No descriptor is opened per event, and the handle identifies the directory even after it is renamed, because the handle encodes the inode rather than a path.

## A whole-filesystem watcher

The program below places a filesystem mark and prints create, delete, and modify events as parent directory plus entry name. Build with `cc -O2 -o fanwatch fanwatch.c`, run as root, and pass any path residing on the target filesystem.

```c
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/fanotify.h>

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: %s <path-on-fs>\n", argv[0]); return 1; }

    int fan = fanotify_init(FAN_CLASS_NOTIF | FAN_REPORT_DFID_NAME, O_RDONLY);
    if (fan < 0) { perror("fanotify_init"); return 1; }

    /* One mark covers the whole superblock; FAN_ONDIR adds events on directories. */
    if (fanotify_mark(fan, FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
                      FAN_CREATE | FAN_DELETE | FAN_MODIFY | FAN_ONDIR,
                      AT_FDCWD, argv[1]) < 0) {
        perror("fanotify_mark"); return 1;
    }

    /* One read() returns a batch of variable-length records, not a single event. */
    char buf[8192];
    for (;;) {
        ssize_t len = read(fan, buf, sizeof buf);
        if (len <= 0) { if (errno == EINTR) continue; break; }

        struct fanotify_event_metadata *m = (struct fanotify_event_metadata *) buf;
        while (FAN_EVENT_OK(m, len)) {
            /* For FID-based notif events the info record follows the metadata. */
            struct fanotify_event_info_fid *fid =
                (struct fanotify_event_info_fid *) (m + 1);
            struct file_handle *fh = (struct file_handle *) fid->handle;
            char *name = (char *) (fh->f_handle + fh->handle_bytes);

            int mount_fd = open(argv[1], O_PATH);
            int dfd = open_by_handle_at(mount_fd, fh, O_PATH);
            close(mount_fd);

            char dpath[PATH_MAX] = "?";
            if (dfd >= 0) {
                char proc[64];
                snprintf(proc, sizeof proc, "/proc/self/fd/%d", dfd);
                ssize_t n = readlink(proc, dpath, sizeof dpath - 1);
                if (n >= 0) dpath[n] = '\0';
                close(dfd);
            }
            printf("mask=0x%llx dir=%s name=%s\n",
                   (unsigned long long) m->mask, dpath,
                   fh->handle_bytes ? name : ".");

            m = FAN_EVENT_NEXT(m, len);
        }
    }
    return 0;
}
```

The load-bearing call is `open_by_handle_at`: the kernel supplies an opaque handle to the *parent directory*, and the handle is converted to a path only when a path is needed. The listing is a sketch of the API shape, not production code. **It resolves a handle and reopens the mount descriptor on every event**, and it reads the info record without first checking `hdr.info_type` to confirm the record layout. A real reader caches handle-to-path mappings, keeps one `O_PATH` descriptor for the mount, and dispatches on `info_type` before casting.

## Permission events: gating opens

Opening a group with `FAN_CLASS_CONTENT` or `FAN_CLASS_PRE_CONTENT` and requesting `FAN_OPEN_PERM` or `FAN_ACCESS_PERM` changes the event from a notification into a question. **The syscall in the accessing process blocks until the daemon writes back a `struct fanotify_response` carrying `FAN_ALLOW` or `FAN_DENY`.** This is the interposition mechanism antivirus and data-loss-prevention agents use without shipping a kernel module.

Two consequences follow directly from that blocking behaviour. The daemon is now on the critical path of every matching open, so a responder that stops reading — or that touches a watched file itself and deadlocks against its own event — stalls the accessing processes. And permission classes and FID reporting are not freely combinable; `fanotify_init(2)` documents which flag combinations are rejected, and that matrix should be consulted for the running kernel rather than assumed.

## Identifying the actor: FAN_REPORT_PIDFD

Event metadata already carries a process identifier (PID), but PIDs are recycled, so a PID read from an event may refer to a different process by the time the reader acts on it. `FAN_REPORT_PIDFD` (since **Linux 5.15**) adds an info record carrying a **pidfd** — a descriptor that refers to a specific process instance. A pidfd is stable against recycling: signalling through `pidfd_send_signal(2)` or inspecting `/proc` through it either targets the original process or fails, never a reincarnation on the same number.

## fatrace

For ad-hoc answers, **fatrace** (Martin Pitt) is a small fanotify command-line tool. It places a mount or filesystem mark, resolves each event to a path, and prints one line per access.

```sh
sudo fatrace --timestamp --seconds 10
```

That reports system-wide file access for ten seconds, which is often enough to identify what is generating disk activity. Its source also serves as a compact reference implementation of the fanotify read loop.

## Pitfalls

- A `FAN_MARK_MOUNT` mark misses accesses through a bind mount or a second mount of the same filesystem; the events are generated against a different mount and never match. `FAN_MARK_FILESYSTEM` (Linux 4.20+) covers the superblock instead.
- Both wide mark types require `CAP_SYS_ADMIN`; without it `fanotify_mark(2)` fails rather than degrading to a narrower scope.
- A single `read()` on the fanotify descriptor returns a **batch of variable-length records**. Treating the buffer as one event, or advancing by `sizeof(struct fanotify_event_metadata)` instead of `FAN_EVENT_NEXT`, silently misparses everything after the first record.
- Under FID reporting the info record layout depends on `hdr.info_type`. Casting to `fanotify_event_info_fid` and reading a name unconditionally produces garbage for records that carry no name.
- `open_by_handle_at(2)` fails when the object has already been deleted — a common case for `FAN_DELETE`, where the handle names something that no longer exists. Path resolution must tolerate the failure rather than treating it as a bug.
- Legacy (non-FID) mode returns **one open file descriptor per event**. A reader that does not close them exhausts its descriptor table under load.
- A permission-event daemon that itself opens a watched file can generate an event it must answer before it can proceed, deadlocking against itself; a stuck or slow responder blocks every accessing process, because their syscalls wait on the response.
- Filesystem-wide marks on a busy root filesystem deliver every matching event on the machine. Filtering is the reader's job, and a reader that cannot keep up loses events unless the group is configured to handle overflow.
