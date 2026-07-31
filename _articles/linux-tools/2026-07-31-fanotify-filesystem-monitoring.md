---
title: "fanotify: watching a whole filesystem instead of one directory at a time"
date: 2026-07-31
track: linux-tools
summary: "inotify makes you recurse and register every directory by hand. fanotify can mark an entire filesystem with one call, report events by file handle, and even gate opens for AV-style scanning. Here's a working FID-based watcher and the exact kernels each flag landed in."
reading_time: 5
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

If you have ever built a filesystem watcher on top of **inotify**, you know the pain: a watch is per-directory, so you walk the tree, add a watch to every directory, race against files created during the walk, and re-add watches as new directories appear. On a large tree you also burn one watch descriptor per directory against `fs.inotify.max_user_watches`.

**fanotify** takes a different shape. Introduced in Linux 2.6.36 and enabled in 2.6.37, it can place a single mark on an entire mount or filesystem and report events for everything underneath it. It also does two things inotify cannot: it can report events by **file handle** (so you get stable identity, not just a name), and it can issue **permission events** that let a userspace daemon allow or deny an open before it completes.

## Mount marks vs filesystem marks

Two mark types give you whole-tree coverage without recursion:

- `FAN_MARK_MOUNT` — watch everything reached *through one mount point*. Cheap, but a bind mount or a second mount of the same fs is a blind spot.
- `FAN_MARK_FILESYSTEM` (since **Linux 4.20**) — watch the whole superblock, regardless of how many mount points expose it. This is the "monitor the entire filesystem" primitive.

Both require `CAP_SYS_ADMIN`. The tradeoff for the wide net is volume: a filesystem mark on a busy `/` will hand you every open, read, and write on the box, so you filter in userspace.

## Reporting by file handle: FAN_REPORT_FID

The classic fanotify mode returns an open file descriptor for each event. That is heavy and, for directory-entry events, doesn't even tell you *what* changed. The modern path is FID reporting:

- `FAN_REPORT_FID` (since **Linux 5.1**) — events carry an `fsid` + `file_handle` instead of an fd.
- `FAN_CREATE`, `FAN_DELETE`, `FAN_MOVE` (since **Linux 5.1**) — the directory-entry events that make fanotify a real inotify replacement.
- `FAN_REPORT_DIR_FID` and `FAN_REPORT_NAME` (both since **Linux 5.9**); `FAN_REPORT_DFID_NAME` is the synonym for the pair, giving you the *parent directory* handle plus the entry name.

With `FAN_REPORT_DFID_NAME` you resolve the parent directory via `open_by_handle_at(2)` and read the name straight out of the event record — no open fd per event, and identity survives renames.

## A working whole-filesystem watcher

This marks a filesystem and prints create/delete/modify events as `parent-dir + name`. Build with `cc -O2 -o fanwatch fanwatch.c`, run as root, pass any path on the target fs.

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

    if (fanotify_mark(fan, FAN_MARK_ADD | FAN_MARK_FILESYSTEM,
                      FAN_CREATE | FAN_DELETE | FAN_MODIFY | FAN_ONDIR,
                      AT_FDCWD, argv[1]) < 0) {
        perror("fanotify_mark"); return 1;
    }

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

The key move is `open_by_handle_at`: the kernel handed you an opaque handle to the *parent directory*, and you turn it into a path on demand. In production you'd cache handle-to-path mappings and check the info record's `hdr.info_type` before trusting the layout, but this is the whole shape of the API.

## Permission events: gating opens

Open a group with `FAN_CLASS_CONTENT` or `FAN_CLASS_PRE_CONTENT` and request `FAN_OPEN_PERM` or `FAN_ACCESS_PERM`, and reads block until your daemon writes back a `struct fanotify_response` with `FAN_ALLOW` or `FAN_DENY`. This is exactly how antivirus and DLP agents interpose on file access without a kernel module. Two cautions: you are now in the critical path of every open, so a stuck responder hangs the system; and mixing permission classes with FID reporting has restrictions — read the man page for the current matrix before combining them.

## Knowing who did it: FAN_REPORT_PIDFD

Event metadata already carries a PID, but PIDs recycle. `FAN_REPORT_PIDFD` (since **Linux 5.15**, backported to **5.10.220**) adds an info record with a **pidfd** for the triggering process, giving you a race-free reference you can `pidfd_send_signal` or read `/proc` through without the "wrong process reincarnated on the same PID" bug.

## When you just want the answer: fatrace

Before writing C, try **fatrace**, Martin Pitt's small fanotify CLI. The 0.19 series added JSON output and flags to report executable paths and parent processes. One line gives you system-wide access events:

```sh
sudo fatrace --timestamp --seconds 10
```

It is the fastest way to answer "what is hammering my disk right now" and a good reference implementation to read before rolling your own.

**Try next:** compile `fanwatch.c` above, point it at `/` with `FAN_MARK_FILESYSTEM`, then `touch /tmp/x && rm /tmp/x` in another shell and watch the create/delete events arrive with no per-directory registration.
