---
title: "Landlock: sandbox a process's filesystem access without root"
date: 2026-07-30
track: linux-tools
summary: "Landlock lets an ordinary, unprivileged program lock itself down — 'I will only ever read /etc and write /tmp' — enforced by the kernel and inherited by every child. Here's the three-syscall model, the no_new_privs requirement, the ABI-versioning dance, and a C program that sandboxes itself before doing risky work."
reading_time: 6
tags: [landlock, lsm, sandboxing, security, seccomp, syscalls]
sources:
  - title: "landlock(7) — Linux manual page"
    url: "https://man7.org/linux/man-pages/man7/landlock.7.html"
  - title: "Landlock: unprivileged access control — The Linux Kernel documentation"
    url: "https://docs.kernel.org/userspace-api/landlock.html"
  - title: "Landlock: Unprivileged Sandboxing (project site)"
    url: "https://landlock.io/"
  - title: "Landlock LSM: kernel documentation"
    url: "https://docs.kernel.org/security/landlock.html"
---

Traditional Linux sandboxing assumes you have power to give away: root sets up a chroot, a namespace, or an AppArmor profile *for* a less-trusted process. **Landlock** inverts that. It's a Linux Security Module that lets an **unprivileged** process restrict *itself* — declare "from this point on I may only read these directories and write those" — and the kernel enforces it, irreversibly, for the process and everything it forks. No root, no config files, no daemon. It's the same "self-imposed jail" idea as seccomp (which filters *syscalls*), but for *filesystem and network access*. Landlock landed in kernel **5.13** and has grown an access right per release since.

## The model: handle, allow, enforce

A Landlock ruleset works by *subtraction from a set you name*. You don't list everything the process can do; you name the categories of access you want the kernel to **handle** (start restricting), then add back **rules** granting specific paths, and anything handled-but-not-granted is denied.

Three syscalls, in order:

1. **`landlock_create_ruleset()`** — declare the `handled_access_fs` bitmask (e.g. "I want to control read, write, and execute on files"). Returns a ruleset file descriptor.
2. **`landlock_add_rule()`** — for each path you *do* want to allow, add a rule: this directory FD, these permitted accesses. Call it once per allowed path.
3. **`landlock_restrict_self()`** — enforce the ruleset on the calling thread. **Irreversible.** From here on, every handled access outside your granted paths gets `EACCES`, and every child inherits the restriction.

Before step 3, an unprivileged process must set **`no_new_privs`** (`prctl(PR_SET_NO_NEW_PRIVS, 1, …)`). This is the same flag seccomp requires: it guarantees the process can't regain privileges via a setuid binary, which is what makes it *safe* for the kernel to let an unprivileged task sandbox itself. Without `CAP_SYS_ADMIN`, `restrict_self` fails unless `no_new_privs` is set.

## ABI versioning: check, don't assume

Landlock's capabilities grow by an **ABI version** you must query at runtime, because your binary might run on an older kernel that lacks the access rights you named. Ask the kernel its supported ABI and mask off anything it doesn't know:

```c
int abi = landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION);
```

The progression (right → first kernel that shipped it), so you can gate features on `abi`:

| ABI | Kernel | Added |
|-----|--------|-------|
| 1 | 5.13 | Core filesystem rights (read/write/execute/read-dir/make-*/remove-*) |
| 2 | 5.19 | `LANDLOCK_ACCESS_FS_REFER` (link/rename across directories) |
| 3 | 6.2  | `LANDLOCK_ACCESS_FS_TRUNCATE` |
| 4 | 6.7  | Network rules: `BIND_TCP`, `CONNECT_TCP` |
| 5 | 6.10 | `LANDLOCK_ACCESS_FS_IOCTL_DEV` (ioctls on device files) |
| 6 | 6.12 | Scoping: abstract UNIX sockets and signals |

If you *require* a right the running kernel is too old to enforce, you decide the policy: fail closed (refuse to run unsandboxed) or degrade (enforce what you can, log the gap). What you must never do is silently assume the sandbox is in force when the kernel quietly ignored a right it didn't recognize.

## A self-sandboxing program

Here's a process that restricts itself to reading `/usr` and `/etc` and writing only `/tmp`, then proves it. The `handled_access_fs` here uses the ABI-1 core rights so it runs on any 5.13+ kernel:

```c
#define _GNU_SOURCE
#include <linux/landlock.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <fcntl.h>
#include <stdio.h>
#include <unistd.h>

static int add_dir(int rs, const char *path, __u64 allowed) {
    struct landlock_path_beneath_attr pb = { .allowed_access = allowed };
    pb.parent_fd = open(path, O_PATH | O_CLOEXEC);
    if (pb.parent_fd < 0) return -1;
    int r = syscall(SYS_landlock_add_rule, rs,
                    LANDLOCK_RULE_PATH_BENEATH, &pb, 0);
    close(pb.parent_fd);
    return r;
}

int main(void) {
    struct landlock_ruleset_attr ra = {
        .handled_access_fs =
            LANDLOCK_ACCESS_FS_READ_FILE  | LANDLOCK_ACCESS_FS_READ_DIR |
            LANDLOCK_ACCESS_FS_WRITE_FILE | LANDLOCK_ACCESS_FS_EXECUTE  |
            LANDLOCK_ACCESS_FS_MAKE_REG,
    };
    int rs = syscall(SYS_landlock_create_ruleset, &ra, sizeof(ra), 0);

    // Grant: read /usr and /etc, read+write+create under /tmp.
    add_dir(rs, "/usr", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR |
                        LANDLOCK_ACCESS_FS_EXECUTE);
    add_dir(rs, "/etc", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR);
    add_dir(rs, "/tmp", LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_WRITE_FILE |
                        LANDLOCK_ACCESS_FS_READ_DIR  | LANDLOCK_ACCESS_FS_MAKE_REG);

    prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0);          // required for unprivileged use
    syscall(SYS_landlock_restrict_self, rs, 0);       // irreversible from here
    close(rs);

    // Proof: writing to /tmp works; touching $HOME does not.
    printf("/tmp/ok     : %s\n", fopen("/tmp/landlock_ok", "w") ? "allowed" : "DENIED");
    printf("$HOME/secret: %s\n", fopen("/root/secret", "w")     ? "allowed" : "DENIED");
    return 0;
}
```

Compile with a recent `linux/landlock.h` (`gcc sandbox.c -o sandbox`) and run it as a normal user. The write under `/tmp` succeeds; the write to your home directory fails with `EACCES` — even though the process's UID has every right to that file. The kernel is enforcing a restriction the process *asked for on itself*.

## Where it fits

Landlock is the right tool when an application wants to shrink its own blast radius before parsing untrusted input, running a plugin, or shelling out — a media transcoder that should only touch its input and output dirs, a build step that shouldn't reach the network, a language runtime sandboxing user scripts. Pair it with **seccomp** (restrict syscalls) and **namespaces** (restrict what's even visible) and you have defence in depth that needs no privileged setup. Its limits: it's path/access-oriented, not a full MAC policy language like SELinux, and older kernels enforce fewer rights — hence the ABI check. But for "this process should only ever touch these files," nothing else is this easy to adopt from inside your own code.

**Try next:** Add `LANDLOCK_ACCESS_FS_TRUNCATE` to the handled set (gate it on `abi >= 3`), grant it on `/tmp` only, then try to `truncate()` a file under `/etc` and watch it fail while `/tmp` truncation succeeds — a concrete feel for how each ABI level widens what you can lock down, and why runtime version-checking isn't optional.
