---
title: "pidfd: race-free process management with file descriptors"
date: 2026-08-07
track: linux-tools
summary: "A PID is a small integer the kernel recycles, so any code that stores one and acts on it later can signal the wrong process. A pidfd is a file descriptor that pins one specific process for its whole lifetime. Here's the PID-reuse race, the syscalls (pidfd_open, CLONE_PIDFD, pidfd_send_signal, pidfd_getfd), waiting with poll/waitid, and a compilable C program that opens, polls, and signals a child."
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

A PID is a small integer, and the kernel recycles it. When a process exits and gets reaped, its PID goes back in the pool; on a busy machine with the default `pid_max` of 32768 it can be handed out again in seconds. So any program that stores a PID and acts on it *later* has a bug waiting to happen: between the moment you looked up the PID and the moment you call `kill()`, the original process may have died and an unrelated one may have inherited its number. You send `SIGKILL` to what you think is a runaway job and take down someone's database instead. This isn't hypothetical — as [LWN put it](https://lwn.net/Articles/784831/), "a stale PID could be used to send a signal to the wrong process," and real security vulnerabilities have come from exactly this race.

A **pidfd** fixes it. Instead of an integer, you hold a *file descriptor* that refers to one specific process. It stays bound to that process for the descriptor's entire lifetime — even after the process dies. The PID number can be reused; your pidfd cannot. Operations through it either hit the right process or fail cleanly. There is no window in which they hit the wrong one.

## Getting a pidfd

Three ways, depending on whether you have the PID or you're creating the process.

**From an existing PID** — `pidfd_open(2)`, since Linux **5.3**:

```c
int syscall(SYS_pidfd_open, pid_t pid, unsigned int flags);
```

It returns a new fd referring to the process `pid`. It fails with `ESRCH` if the process is already gone, which is itself the race-free answer to "does this process still exist?" The `flags` argument takes `PIDFD_NONBLOCK` (Linux 5.10) to make `waitid()` return `EAGAIN` instead of blocking, and `PIDFD_THREAD` (Linux 6.9) to refer to a single thread.

**At creation time** — `CLONE_PIDFD`, since Linux **5.2**. This closes the last gap: even `pidfd_open()` has a theoretical window between fork and open. With `clone3(2)` (Linux 5.3) you ask the kernel to hand you the pidfd atomically as the child is born, by pointing `clone_args.pidfd` at an `int` and setting the flag:

```c
int pidfd = -1;
struct clone_args args = {
    .flags  = CLONE_PIDFD,
    .pidfd  = (unsigned long long)(uintptr_t)&pidfd,
    .exit_signal = SIGCHLD,
};
pid_t pid = syscall(SYS_clone3, &args, sizeof(args));
```

**From another process's fd table** — `pidfd_getfd(2)`, more on that below.

You can see a pidfd in `/proc` like any other descriptor. It shows up as an anonymous inode, and its `fdinfo` names the process it points at:

```console
$ ls -l /proc/$$/fd/5
lrwx------ 1 user user 64 ... /proc/1234/fd/5 -> anon_inode:[pidfd]
$ grep Pid /proc/1234/fdinfo/5
Pid:    9876
```

## Signaling and waiting

Once you hold a pidfd, `pidfd_send_signal(2)` (the syscall that started it all, Linux **5.1**) replaces `kill()`:

```c
int syscall(SYS_pidfd_send_signal, int pidfd, int sig,
            siginfo_t *info, unsigned int flags);
```

Pass `NULL` for `info` and `0` for `flags` and it behaves like `kill(pid, sig)` — except it can never signal the wrong process. If the target has exited, you get `ESRCH`, not a misfire.

The other half is knowing *when* the process exits, and here the file-descriptor design pays off twice. A pidfd is **pollable**, since Linux **5.3**: hand it to `poll(2)`, `select(2)`, or `epoll(7)` and it becomes readable (`EPOLLIN`) the moment the process becomes a zombie. That means process death slots into an event loop next to your sockets and timers — no `SIGCHLD` handler, no `signalfd`, no self-pipe. (There's nothing useful to `read()`; the readiness *is* the signal.) To actually reap the child and collect its exit status, `waitid(2)` grew a `P_PIDFD` idtype in Linux **5.4**:

```c
waitid(P_PIDFD, pidfd, &info, WEXITED);
```

Here the `id` argument is the pidfd itself. A supervisor can therefore watch dozens of children in one `epoll` loop and reap each one race-free as its fd fires.

## A program that ties it together

This forks a child, opens a pidfd for it, sends `SIGTERM` through the pidfd, waits for exit via `poll()`, and reaps it with `waitid(P_PIDFD, …)`. It compiles with `gcc pidfd.c -o pidfd` on any glibc and runs on a 5.4+ kernel.

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

    /* This fd pins THIS process. Even if pid is recycled, we can't misfire. */
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

Every step here is race-free by construction. There is no point at which an integer PID is dereferenced against a live process table.

## Stealing a descriptor: pidfd_getfd

The most surprising member of the family is `pidfd_getfd(2)`, Linux **5.6**:

```c
int syscall(SYS_pidfd_getfd, int pidfd, int targetfd, unsigned int flags);
```

Given a pidfd and a descriptor *number* `targetfd` that is open in that other process, it installs a **duplicate of that descriptor** into your own fd table — same open file description, shared offset and flags. It's `dup()` across a process boundary. Access is gated by a `PTRACE_MODE_ATTACH_REALCREDS` check, so you need the same privilege you'd need to `ptrace` the target. The motivating use, described in [LWN](https://lwn.net/Articles/808997/), is the seccomp user-space notifier: a supervisor intercepts a sandboxed process's syscall, and to service something like `connect()` on the sandbox's behalf it needs the *actual socket* the sandbox opened — `pidfd_getfd()` reaches in and grabs it. Debuggers and container managers use it the same way.

## glibc and portability

Historically glibc shipped no wrappers for any of these, so the man pages show raw `syscall(2)` calls — which is also the most portable thing to write, since it works regardless of your libc version. glibc **2.36** (2022) added `pidfd_open()` and `pidfd_getfd()` wrappers, but `pidfd_send_signal()` still has none. Define the tiny static wrappers above and you sidestep the whole question. Note that pidfds are namespace-aware: a pidfd for a process in another PID namespace still works, because it doesn't depend on a number that only means something inside one namespace — another reason the file-descriptor handle is the right primitive for container tooling.

**Try next:** Rewrite the program to use `clone3()` with `CLONE_PIDFD` instead of `fork()` + `pidfd_open()`, so you get the pidfd atomically at birth — then add a second child and drive both to exit through a single `epoll` loop, reaping each with `waitid(P_PIDFD, …)` as its fd fires. That's the exact shape of a race-free process supervisor.
