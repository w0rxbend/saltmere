---
title: "pidfd: race-free process management with file descriptors"
date: 2026-08-07
track: linux-tools
summary: "A process identifier (PID) is a small integer the kernel recycles, so code that stores one and acts on it later can signal the wrong process. A pidfd is a file descriptor that pins one specific process for the descriptor's whole lifetime. This article covers the PID-reuse race, the syscalls (pidfd_open, CLONE_PIDFD, pidfd_send_signal, pidfd_getfd), waiting with poll and waitid, and a compilable C program that opens, polls, and signals a child."
reading_time: 6
tags: [linux, pidfd, process-management, syscalls, signals]
sources:
  - title: "pidfd_open(2) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man2/pidfd_open.2.html"
  - title: "pidfd_send_signal(2) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html"
  - title: "pidfd_getfd(2) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man2/pidfd_getfd.2.html"
  - title: "Rethinking race-free process signaling — LWN.net"
    url: "https://lwn.net/Articles/784831/"
  - title: "Grabbing file descriptors with pidfd_getfd() — LWN.net"
    url: "https://lwn.net/Articles/808997/"
---

**Gist.** A process identifier (PID) is a recycled integer, so the interval between reading a PID and acting on it is a window in which the original process can exit and an unrelated process can inherit the number — the classic stale-PID misfire. A **pidfd** replaces the integer with a file descriptor bound to one specific process for the descriptor's entire lifetime, so every operation through it either reaches that process or fails with `ESRCH`. The cost is a per-process open descriptor, and a set of syscalls with staggered kernel-version floors (Linux 5.1 through 6.9) that a portable caller must either probe for or invoke through raw `syscall(2)`.

## The race the descriptor removes

When a process exits and is reaped, its PID returns to the allocation pool. PIDs are handed out cyclically up to `pid_max`, whose traditional default is 32768, so on a machine that churns through processes the counter can wrap and reissue a number quickly. Code that caches a PID and later calls `kill()` therefore carries an unguarded interval: the process observed at lookup time need not be the process addressed at signal time. [LWN's account of the pidfd work](https://lwn.net/Articles/784831/) frames this stale-PID signalling as the problem the API set out to remove.

The **invariant a pidfd supplies** is that the binding between descriptor and process is fixed at descriptor creation and never re-resolved. The PID number may be reused; the descriptor is not affected, because it does not name a number. **A pidfd remains valid after the target exits** — once the target is gone, operations through the descriptor fail rather than reaching whichever process later holds the same number.

## Obtaining a pidfd

Three acquisition paths exist, differing in whether the caller already holds a PID or is creating the process.

**From an existing PID** — `pidfd_open(2)`, since Linux **5.3**:

```c
int syscall(SYS_pidfd_open, pid_t pid, unsigned int flags);
```

The call returns a new descriptor referring to process `pid`, and fails with **`ESRCH` if no such process exists**, which doubles as a race-free existence test. The `flags` argument accepts `PIDFD_NONBLOCK` (Linux **5.10**), which makes `waitid()` return `EAGAIN` rather than blocking, and `PIDFD_THREAD` (Linux **6.9**), which makes the descriptor refer to a single thread.

**At creation time** — `CLONE_PIDFD`, since Linux **5.2**. This closes the residual window in the `pidfd_open()` path: between the return of a fork and the subsequent open, a `SIGCHLD` handler in the same process can reap the child, releasing its PID for reuse before the descriptor is ever created. With `clone3(2)` (Linux **5.3**) the kernel installs the descriptor atomically as the child is created, given a pointer to an `int` in `clone_args.pidfd`:

```c
int pidfd = -1;
struct clone_args args = {
    .flags  = CLONE_PIDFD,
    .pidfd  = (unsigned long long)(uintptr_t)&pidfd,
    .exit_signal = SIGCHLD,
};
pid_t pid = syscall(SYS_clone3, &args, sizeof(args));
```

**From another process's descriptor table** — `pidfd_getfd(2)`, described below.

A pidfd is visible in `/proc` like any other descriptor. It appears as an anonymous inode, and its `fdinfo` entry names the target process:

```console
$ ls -l /proc/1234/fd/5
lrwx------ 1 user user 64 ... /proc/1234/fd/5 -> anon_inode:[pidfd]
$ grep Pid /proc/1234/fdinfo/5
Pid:    9876
```

## Signalling and waiting

`pidfd_send_signal(2)` — the first syscall of the family, Linux **5.1** — replaces `kill()`:

```c
int syscall(SYS_pidfd_send_signal, int pidfd, int sig,
            siginfo_t *info, unsigned int flags);
```

With `NULL` for `info` and `0` for `flags` the semantics match `kill(pid, sig)`, with the difference that the target cannot be a different process from the one the descriptor was opened for. **Once the target has been reaped, the call fails with `ESRCH` rather than reaching a successor;** a target that has exited but is still a zombie is a valid destination, exactly as it is for `kill()`.

Exit notification uses the same descriptor. A pidfd is **pollable since Linux 5.3**: passed to `poll(2)`, `select(2)` or `epoll(7)`, it becomes readable (`EPOLLIN`) **at the moment the process becomes a zombie**. Process death therefore enters an event loop alongside sockets and timers, without a `SIGCHLD` handler, `signalfd`, or self-pipe. There is no payload to `read()`; readiness is the entire notification. Reaping and exit-status collection use `waitid(2)`, which gained the `P_PIDFD` idtype in Linux **5.4**:

```c
waitid(P_PIDFD, pidfd, &info, WEXITED);
```

The `id` argument is the descriptor itself. A supervisor can consequently hold many children in one `epoll` set and reap each one race-free as its descriptor fires.

## A program that combines the pieces

The following forks a child, opens a pidfd for it, sends `SIGTERM` through the descriptor, waits for exit with `poll()`, and reaps with `waitid(P_PIDFD, …)`. It compiles with `gcc pidfd.c -o pidfd` on glibc and runs on a 5.4 or later kernel.

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <poll.h>
#include <signal.h>
#include <sys/syscall.h>
#include <sys/wait.h>

#ifndef P_PIDFD
#define P_PIDFD 3            /* value fixed by the kernel ABI */
#endif

static int pidfd_open(pid_t pid, unsigned int flags) {
    return syscall(SYS_pidfd_open, pid, flags);
}
static int pidfd_send_signal(int pidfd, int sig,
                             siginfo_t *info, unsigned int flags) {
    return syscall(SYS_pidfd_send_signal, pidfd, sig, info, flags);
}

int main(void) {
    pid_t pid = fork();
    if (pid == 0) { pause(); _exit(0); }   /* child sleeps until signaled */

    int pidfd = pidfd_open(pid, 0);
    if (pidfd < 0) { perror("pidfd_open"); exit(1); }

    /* The fd pins THIS process; a recycled pid cannot be reached through it. */
    if (pidfd_send_signal(pidfd, SIGTERM, NULL, 0) < 0) {
        perror("pidfd_send_signal"); exit(1);
    }

    /* Readable (EPOLLIN) once the child becomes a zombie. */
    struct pollfd pfd = { .fd = pidfd, .events = POLLIN };
    if (poll(&pfd, 1, -1) < 0) { perror("poll"); exit(1); }

    /* Reap it; waitid understands the pidfd directly (Linux 5.4+). */
    siginfo_t si = {0};
    if (waitid(P_PIDFD, pidfd, &si, WEXITED) < 0) {
        perror("waitid"); exit(1);
    }
    printf("child %d exited: code=%d status=%d\n",
           pid, si.si_code, si.si_status);
    close(pidfd);
    return 0;
}
```

After the `pidfd_open()` call succeeds, no step dereferences an integer PID against the live process table. The residual exposure is the interval between `fork()` returning and `pidfd_open()` succeeding, which the `CLONE_PIDFD` path removes entirely.

## Transferring a descriptor: pidfd_getfd

`pidfd_getfd(2)`, Linux **5.6**, operates on the target's descriptor table rather than on the process itself:

```c
int syscall(SYS_pidfd_getfd, int pidfd, int targetfd, unsigned int flags);
```

Given a pidfd and a descriptor *number* `targetfd` open in that process, the call installs a **duplicate of that descriptor** in the caller's own table: the same open file description, with shared offset and flags. The effect is `dup()` across a process boundary. Access is gated by a **`PTRACE_MODE_ATTACH_REALCREDS` check**, so the caller needs the privilege required to `ptrace` the target. The use described in [LWN](https://lwn.net/Articles/808997/) is the seccomp user-space notifier: a supervisor intercepts a sandboxed process's syscall, and servicing an operation such as `connect()` on the sandbox's behalf requires the actual socket the sandbox opened. Debuggers and container managers use the call in the same way.

## glibc coverage and namespaces

glibc shipped no wrappers for these calls for several releases after the syscalls landed, which is why the manual pages show raw `syscall(2)` invocations. glibc **2.36** (2022) added wrappers for the family, including `pidfd_open()`, `pidfd_send_signal()` and `pidfd_getfd()`. **The raw `syscall(2)` form remains the portable one**, because it does not depend on the libc version the program is built against — which is why the example above defines its own static wrappers. Pidfds are also namespace-aware: a pidfd for a process in another PID namespace continues to work, because the handle does not carry a number whose meaning is confined to a single namespace.

## Pitfalls

- **A cached PID passed to `kill()` can signal a successor process.** The PID is released back to the pool at reap time, and once the cyclic allocator wraps past `pid_max` the same number is issued again to an unrelated process.
- **`fork()` followed by `pidfd_open()` still leaves a window.** A `SIGCHLD` handler that reaps the child before the open runs frees its PID, so the open either fails with `ESRCH` or binds a successor; only `clone3()` with `CLONE_PIDFD` yields the descriptor atomically.
- **`read()` on a pidfd returns nothing useful.** Readiness under `poll`/`epoll` is the notification; code that waits for readable data will not obtain an exit status that way.
- **A readable pidfd means zombie, not reaped.** The exit status is collected only by `waitid(P_PIDFD, …)`, and the entry persists until then.
- **`waitid()` on a pidfd whose target is still running blocks unless the descriptor is non-blocking or `WNOHANG` is passed.** `PIDFD_NONBLOCK`, available from Linux 5.10, turns that wait into an `EAGAIN` return.
- **Calling a pidfd function by name on a pre-2.36 glibc fails to link.** The wrappers arrived in glibc 2.36; a build targeting an older libc has to issue the calls through `syscall(2)`.
- **`pidfd_getfd()` fails without ptrace-level privilege over the target.** The `PTRACE_MODE_ATTACH_REALCREDS` check applies, so an unprivileged supervisor cannot pull descriptors from an arbitrary process.
- **A syscall used below its kernel-version floor returns `ENOSYS`.** The floors differ within the family: 5.1 for `pidfd_send_signal()`, 5.2 for `CLONE_PIDFD`, 5.3 for `pidfd_open()` and pollability, 5.4 for `P_PIDFD`, 5.6 for `pidfd_getfd()`.
